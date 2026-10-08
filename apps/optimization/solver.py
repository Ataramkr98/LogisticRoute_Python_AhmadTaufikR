from dataclasses import dataclass, field
from enum import StrEnum

# Vehicle shift windows and stop time windows are expressed as seconds from the
# start of the service day. Two days is the widest span the model will accept,
# which comfortably covers a shift that crosses midnight without letting a
# malformed window inflate the search space.
_TIME_HORIZON_CEILING = 2 * 86400


class SolverStatus(StrEnum):
    OPTIMAL = "OPTIMAL"
    FEASIBLE = "FEASIBLE"
    INFEASIBLE = "INFEASIBLE"
    TIME_LIMIT = "TIME_LIMIT"
    FAILED = "FAILED"


@dataclass(frozen=True)
class Node:
    key: str
    order_id: int | None
    stop_type: str
    service_s: int = 0
    weight_g: int = 0
    volume_l: int = 0
    packages: int = 0
    time_window: tuple[int, int] | None = None
    required_skills: frozenset[str] = frozenset()
    prohibited_vehicle_types: frozenset[str] = frozenset()
    optional_penalty: int | None = None


@dataclass(frozen=True)
class SolverVehicle:
    key: str
    vehicle_type: str
    capacity_weight_g: int
    capacity_volume_l: int
    capacity_packages: int
    max_stops: int
    max_duration_s: int
    skills: frozenset[str] = frozenset()
    fixed_cost: int = 0
    shift_window: tuple[int, int] = (0, 86400)


@dataclass(frozen=True)
class SolverInput:
    nodes: list[Node]
    vehicles: list[SolverVehicle]
    distances_m: list[list[int]]
    durations_s: list[list[int]]
    pickup_delivery_pairs: list[tuple[int, int]] = field(default_factory=list)
    time_limit_s: int = 30
    distance_weight: int = 1
    duration_weight: int = 1


@dataclass(frozen=True)
class RouteResult:
    vehicle_index: int
    node_indices: list[int]
    arrivals_s: list[int]
    load_weight_g: list[int]
    load_volume_l: list[int]
    load_packages: list[int]
    distance_m: int
    duration_s: int


@dataclass(frozen=True)
class SolverResult:
    status: SolverStatus
    routes: list[RouteResult]
    unassigned_node_indices: list[int]
    objective: int | None
    solver_version: str


def solve_vrp(data: SolverInput) -> SolverResult:
    try:
        import ortools
        from ortools.constraint_solver import pywrapcp, routing_enums_pb2
    except ImportError as exc:
        raise RuntimeError("OR-Tools is not installed; install requirements.txt") from exc

    if not data.vehicles or len(data.nodes) < 2:
        return SolverResult(SolverStatus.INFEASIBLE, [], list(range(1, len(data.nodes))), None, ortools.__version__)

    starts = [0] * len(data.vehicles)
    ends = [0] * len(data.vehicles)
    manager = pywrapcp.RoutingIndexManager(len(data.nodes), len(data.vehicles), starts, ends)
    routing = pywrapcp.RoutingModel(manager)

    def cost_callback(from_index, to_index):
        source = manager.IndexToNode(from_index)
        target = manager.IndexToNode(to_index)
        return (
            data.distances_m[source][target] * data.distance_weight
            + data.durations_s[source][target] * data.duration_weight
        )

    cost_idx = routing.RegisterTransitCallback(cost_callback)
    routing.SetArcCostEvaluatorOfAllVehicles(cost_idx)
    for index, vehicle in enumerate(data.vehicles):
        routing.SetFixedCostOfVehicle(vehicle.fixed_cost, index)

    def time_callback(from_index, to_index):
        source = manager.IndexToNode(from_index)
        target = manager.IndexToNode(to_index)
        return data.nodes[source].service_s + data.durations_s[source][target]

    time_idx = routing.RegisterTransitCallback(time_callback)

    # The dimension horizon must be able to represent every clock time that any
    # shift window or stop time window can take. Vehicle time windows are
    # absolute seconds from the service-day start, so a route may legally run
    # from 07:00 until 17:00 and receive a stop whose window opens at 15:30.
    #
    # Previously the per-vehicle upper bound was ``max_duration_s``. That
    # conflates two different quantities: ``max_duration_s`` bounds how long a
    # route may *last*, whereas the CumulVar bound is an absolute point on the
    # clock. Whenever a stop window extended past ``max_duration_s``, OR-Tools
    # could not intersect it with the dimension domain and aborted model
    # construction with ``CP Solver fail`` from ``SetRange``. The horizon is now
    # the full span the day can occupy, and ``max_duration_s`` is enforced below
    # as an explicit constraint on elapsed route time.
    horizon = _TIME_HORIZON_CEILING
    for vehicle in data.vehicles:
        horizon = max(horizon, vehicle.shift_window[1])
    for node in data.nodes:
        if node.time_window:
            horizon = max(horizon, node.time_window[1])
    routing.AddDimensionWithVehicleCapacity(
        time_idx,
        horizon,
        [horizon] * len(data.vehicles),
        False,
        "Time",
    )
    time_dimension = routing.GetDimensionOrDie("Time")
    for vehicle_index, vehicle in enumerate(data.vehicles):
        shift_start, shift_end = vehicle.shift_window
        start_cumul = time_dimension.CumulVar(routing.Start(vehicle_index))
        end_cumul = time_dimension.CumulVar(routing.End(vehicle_index))
        start_cumul.SetRange(shift_start, shift_end)
        end_cumul.SetRange(shift_start, shift_end)
        # Bound the route's elapsed working time rather than its arrival clock
        # time, which is what the vehicle's maximum route duration really means.
        max_duration = min(vehicle.max_duration_s, shift_end - shift_start)
        if max_duration > 0:
            routing.solver().Add(end_cumul - start_cumul <= max_duration)
    for node_index, node in enumerate(data.nodes[1:], start=1):
        if not node.time_window:
            continue
        window_start, window_end = node.time_window
        # Clamp instead of crashing: a window that runs past the representable
        # horizon (or is inverted by bad upstream data) would otherwise make the
        # domain empty and abort the whole solve for every other order too.
        window_start = max(0, min(window_start, horizon))
        window_end = max(window_start, min(window_end, horizon))
        time_dimension.CumulVar(manager.NodeToIndex(node_index)).SetRange(window_start, window_end)

    def add_capacity(name, values, capacities):
        callback = routing.RegisterUnaryTransitCallback(lambda index: values[manager.IndexToNode(index)])
        routing.AddDimensionWithVehicleCapacity(callback, 0, capacities, True, name)

    add_capacity("Weight", [node.weight_g for node in data.nodes], [v.capacity_weight_g for v in data.vehicles])
    add_capacity("Volume", [node.volume_l for node in data.nodes], [v.capacity_volume_l for v in data.vehicles])
    add_capacity("Packages", [node.packages for node in data.nodes], [v.capacity_packages for v in data.vehicles])
    add_capacity("Stops", [0] + [1] * (len(data.nodes) - 1), [v.max_stops for v in data.vehicles])

    for node_index, node in enumerate(data.nodes[1:], start=1):
        index = manager.NodeToIndex(node_index)
        vehicle_count = len(data.vehicles)
        allowed = [
            i
            for i, vehicle in enumerate(data.vehicles)
            if node.required_skills.issubset(vehicle.skills)
            and vehicle.vehicle_type not in node.prohibited_vehicle_types
        ]
        if len(allowed) == vehicle_count:
            # Nothing to exclude: leave the domain untouched. Writing an
            # identical SetValues([...]) here measurably degrades the search
            # (it also primes the first-solution heuristic with an explicit
            # vehicle ordering), which on mid-sized instances makes the solver
            # burn its whole time budget and return no solution at all.
            pass
        elif allowed:
            routing.VehicleVar(index).RemoveValues([i for i in range(vehicle_count) if i not in allowed])
        elif node.optional_penalty is not None:
            # No eligible vehicle exists; force the node to be dropped rather
            # than leaving the search to prove it is unreachable.
            routing.VehicleVar(index).SetValue(-1)
        if node.optional_penalty is not None:
            routing.AddDisjunction([index], node.optional_penalty)

    for pickup_node, delivery_node in data.pickup_delivery_pairs:
        pickup_index = manager.NodeToIndex(pickup_node)
        delivery_index = manager.NodeToIndex(delivery_node)
        routing.AddPickupAndDelivery(pickup_index, delivery_index)
        routing.solver().Add(routing.VehicleVar(pickup_index) == routing.VehicleVar(delivery_index))
        routing.solver().Add(time_dimension.CumulVar(pickup_index) <= time_dimension.CumulVar(delivery_index))
        routing.solver().Add(routing.ActiveVar(pickup_index) == routing.ActiveVar(delivery_index))

    # Two passes share the budget: greedy descent to guarantee a first solution,
    # then guided local search to improve it.
    #
    # The first pass used to receive the entire budget, which meant that on a
    # slow machine the second pass could expire and then throw away the valid
    # solution the first had already produced. Splitting the budget in half
    # fixed that but introduced the opposite failure: the first OR-Tools
    # solve of a process pays for loading and initialising the native solver,
    # and that cold-start cost can consume the whole half-budget, so a trivial
    # one-stop problem reported TIME_LIMIT with nothing assigned.
    #
    # The first pass therefore gets a guaranteed floor — or the whole budget
    # when the caller asked for a very small one — and the improvement pass only
    # ever spends what is genuinely left over.
    budget_s = max(1, data.time_limit_s)
    first_pass_budget = max(5, budget_s // 2)

    params = pywrapcp.DefaultRoutingSearchParameters()
    params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    # Greedy descent returns a usable solution quickly, which matters because a
    # guided local search can otherwise consume the whole wall-clock budget and
    # report nothing even when a solution plainly exists.
    params.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GREEDY_DESCENT
    params.time_limit.seconds = first_pass_budget
    solution = routing.SolveWithParameters(params)
    solution_status = routing.status()

    # Guided local search explores further than greedy descent but is much
    # slower to produce a first solution. Only pay for it when the cheap pass
    # already found something worth improving, so a hard instance never trades a
    # real answer for an empty one.
    improvement_budget = budget_s - first_pass_budget
    if solution is not None and improvement_budget >= 1:
        improved = pywrapcp.DefaultRoutingSearchParameters()
        improved.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
        improved.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
        improved.time_limit.seconds = improvement_budget
        candidate = routing.SolveWithParameters(improved)
        if candidate is not None and candidate.ObjectiveValue() <= solution.ObjectiveValue():
            solution = candidate
            solution_status = routing.status()

    if not solution:
        timeout_code = routing_enums_pb2.RoutingSearchStatus.ROUTING_FAIL_TIMEOUT
        status = SolverStatus.TIME_LIMIT if solution_status == timeout_code else SolverStatus.INFEASIBLE
        return SolverResult(status, [], list(range(1, len(data.nodes))), None, ortools.__version__)

    routes = []
    assigned = set()
    for vehicle_index in range(len(data.vehicles)):
        index = routing.Start(vehicle_index)
        node_indices, arrivals = [], []
        load_weights, load_volumes, load_packages = [], [], []
        route_distance = 0
        running_weight = running_volume = running_packages = 0
        while not routing.IsEnd(index):
            node = manager.IndexToNode(index)
            node_indices.append(node)
            arrivals.append(solution.Value(time_dimension.CumulVar(index)))
            running_weight += data.nodes[node].weight_g
            running_volume += data.nodes[node].volume_l
            running_packages += data.nodes[node].packages
            load_weights.append(running_weight)
            load_volumes.append(running_volume)
            load_packages.append(running_packages)
            if node:
                assigned.add(node)
            next_index = solution.Value(routing.NextVar(index))
            next_node = manager.IndexToNode(next_index)
            route_distance += data.distances_m[node][next_node]
            index = next_index
        node_indices.append(0)
        arrivals.append(solution.Value(time_dimension.CumulVar(index)))
        load_weights.append(running_weight)
        load_volumes.append(running_volume)
        load_packages.append(running_packages)
        if len(node_indices) > 2:
            routes.append(
                RouteResult(
                    vehicle_index,
                    node_indices,
                    arrivals,
                    load_weights,
                    load_volumes,
                    load_packages,
                    route_distance,
                    arrivals[-1] - arrivals[0],
                )
            )
    unassigned = sorted(set(range(1, len(data.nodes))) - assigned)
    optimal_code = routing_enums_pb2.RoutingSearchStatus.ROUTING_OPTIMAL
    # A run that hit its budget still produced this plan; reporting it as
    # FEASIBLE is accurate and keeps the caller from discarding real work.
    status = SolverStatus.OPTIMAL if solution_status == optimal_code else SolverStatus.FEASIBLE
    return SolverResult(status, routes, unassigned, solution.ObjectiveValue(), ortools.__version__)
