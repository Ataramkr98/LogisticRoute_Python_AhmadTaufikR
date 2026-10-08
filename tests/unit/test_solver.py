from apps.optimization.solver import (
    Node,
    SolverInput,
    SolverStatus,
    SolverVehicle,
    solve_vrp,
)


def vehicle(**overrides):
    values = {
        "key": "vehicle-1",
        "vehicle_type": "VAN",
        "capacity_weight_g": 10_000,
        "capacity_volume_l": 100,
        "capacity_packages": 10,
        "max_stops": 10,
        "max_duration_s": 7200,
        "skills": frozenset({"STANDARD"}),
        "shift_window": (0, 7200),
    }
    values.update(overrides)
    return SolverVehicle(**values)


def input_for(nodes, vehicles=None, pairs=None):
    size = len(nodes)
    distances = [[0 if row == col else 1000 + abs(row - col) * 100 for col in range(size)] for row in range(size)]
    durations = [[0 if row == col else 120 + abs(row - col) * 10 for col in range(size)] for row in range(size)]
    return SolverInput(
        nodes=nodes,
        vehicles=vehicles or [vehicle()],
        distances_m=distances,
        durations_s=durations,
        pickup_delivery_pairs=pairs or [],
        time_limit_s=1,
    )


def test_solver_returns_route_and_cumulative_load():
    nodes = [
        Node("depot", None, "DEPOT"),
        Node("delivery", 1, "DELIVERY", weight_g=2500, volume_l=4, packages=2, optional_penalty=100_000),
    ]
    result = solve_vrp(input_for(nodes))
    assert result.status in {SolverStatus.OPTIMAL, SolverStatus.FEASIBLE}
    assert result.routes[0].node_indices == [0, 1, 0]
    assert result.routes[0].load_weight_g == [0, 2500, 2500]


def test_capacity_breach_produces_explainable_unassigned_node():
    nodes = [
        Node("depot", None, "DEPOT"),
        Node("heavy", 1, "DELIVERY", weight_g=20_000, packages=1, optional_penalty=100_000),
    ]
    result = solve_vrp(input_for(nodes))
    assert result.unassigned_node_indices == [1]
    assert result.routes == []


def test_skill_requirement_excludes_ineligible_vehicle():
    nodes = [
        Node("depot", None, "DEPOT"),
        Node(
            "cold",
            1,
            "DELIVERY",
            weight_g=1000,
            packages=1,
            required_skills=frozenset({"COLD_CHAIN"}),
            optional_penalty=100_000,
        ),
    ]
    result = solve_vrp(input_for(nodes))
    assert result.unassigned_node_indices == [1]


def test_pickup_is_scheduled_before_delivery():
    nodes = [
        Node("depot", None, "DEPOT"),
        Node("pickup", 1, "PICKUP", weight_g=1000, packages=1, optional_penalty=100_000),
        Node("delivery", 1, "DELIVERY", weight_g=-1000, packages=-1, optional_penalty=100_000),
    ]
    result = solve_vrp(input_for(nodes, pairs=[(1, 2)]))
    route = result.routes[0].node_indices
    assert route.index(1) < route.index(2)


def test_impossible_time_window_drops_optional_order():
    nodes = [
        Node("depot", None, "DEPOT"),
        Node(
            "late",
            1,
            "DELIVERY",
            weight_g=1000,
            packages=1,
            time_window=(10, 20),
            optional_penalty=100_000,
        ),
    ]
    result = solve_vrp(input_for(nodes))
    assert result.unassigned_node_indices == [1]


def test_time_window_beyond_max_route_duration_does_not_abort_solve():
    """A stop window reaching past ``max_duration_s`` must stay representable.

    Vehicle windows are absolute seconds from the start of the service day while
    ``max_duration_s`` bounds how long a route may last. Using the latter as the
    time dimension horizon made any later window unsatisfiable, which OR-Tools
    reported as ``CP Solver fail`` while the model was still being built.
    """
    nodes = [
        Node("depot", None, "DEPOT"),
        Node(
            "morning",
            1,
            "DELIVERY",
            weight_g=1000,
            packages=1,
            time_window=(28_800, 39_600),  # 08:00-11:00
            optional_penalty=100_000,
        ),
        Node(
            "afternoon",
            2,
            "DELIVERY",
            weight_g=1000,
            packages=1,
            time_window=(45_000, 55_800),  # 12:30-15:30, well past a 10h budget
            optional_penalty=100_000,
        ),
    ]
    tight = vehicle(max_duration_s=36_000, shift_window=(25_200, 61_200))
    result = solve_vrp(input_for(nodes, vehicles=[tight]))
    assert result.status in {SolverStatus.OPTIMAL, SolverStatus.FEASIBLE}


def test_max_route_duration_bounds_elapsed_route_time():
    """``max_duration_s`` limits elapsed time, not arrival clock time.

    With a generous shift but a tight duration budget the solver must stop
    serving stops once the elapsed time would exceed the budget, instead of
    treating the budget as an absolute deadline on the clock.
    """
    nodes = [Node("depot", None, "DEPOT")] + [
        Node(f"stop-{index}", index, "DELIVERY", weight_g=1, packages=1, optional_penalty=100_000)
        for index in range(1, 5)
    ]
    tight = vehicle(max_duration_s=300, shift_window=(0, 86_400))
    result = solve_vrp(input_for(nodes, vehicles=[tight]))
    assert result.status in {SolverStatus.OPTIMAL, SolverStatus.FEASIBLE}
    for route in result.routes:
        assert route.duration_s <= 300


def test_stop_window_outside_shift_is_dropped_rather_than_crashing():
    nodes = [
        Node("depot", None, "DEPOT"),
        Node(
            "before-shift",
            1,
            "DELIVERY",
            weight_g=1000,
            packages=1,
            time_window=(3600, 7200),  # 01:00-02:00, shift starts at 07:00
            optional_penalty=100_000,
        ),
    ]
    result = solve_vrp(input_for(nodes, vehicles=[vehicle(shift_window=(25_200, 61_200))]))
    assert result.status in {SolverStatus.OPTIMAL, SolverStatus.FEASIBLE}
    assert result.unassigned_node_indices == [1]


def test_found_solution_is_never_discarded_when_the_search_hits_its_budget():
    """A timeout must not throw away a plan the solver already produced.

    The search runs two passes (greedy descent, then guided local search).
    Handing each pass the full budget meant a slow or cold machine could let the
    second pass expire and then discard the valid solution found by the first,
    silently reporting every order as unassigned. Every pass now gets a slice of
    the budget, and a run that exhausts its slice still returns the plan it
    built rather than an empty result.
    """
    nodes = [Node("depot", None, "DEPOT")] + [
        Node(f"stop-{index}", index, "DELIVERY", weight_g=10, packages=1, optional_penalty=100_000)
        for index in range(1, 40)
    ]
    vehicles = [
        vehicle(
            key=f"vehicle-{index}",
            capacity_weight_g=100_000,
            capacity_volume_l=1_000,
            capacity_packages=100,
            max_stops=100,
            max_duration_s=86_400,
            shift_window=(0, 86_400),
        )
        for index in range(3)
    ]
    result = solve_vrp(input_for(nodes, vehicles=vehicles))
    assert result.status in {SolverStatus.OPTIMAL, SolverStatus.FEASIBLE}
    served = {node for route in result.routes for node in route.node_indices}
    assert 0 in served
    # A timeout may leave work unassigned, but it must never report progress as
    # an empty plan while claiming the orders were undeliverable.
    if result.unassigned_node_indices:
        assert result.routes, "unassigned orders must not come with an empty plan"

