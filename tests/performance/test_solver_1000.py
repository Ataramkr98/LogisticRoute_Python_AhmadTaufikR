import os

import pytest

from apps.optimization.solver import Node, SolverInput, SolverVehicle, solve_vrp

pytestmark = [
    pytest.mark.performance,
    pytest.mark.skipif(os.getenv("RUN_PERFORMANCE_TESTS") != "1", reason="Opt in to scale tests"),
]


def test_one_thousand_order_solver_is_bounded():
    nodes = [Node("depot", None, "DEPOT")]
    nodes.extend(
        Node(f"order-{index}", index, "DELIVERY", weight_g=1000, packages=1, optional_penalty=1_000_000)
        for index in range(1, 1001)
    )
    size = len(nodes)
    distances = [[0 if row == col else 1000 + abs(row - col) for col in range(size)] for row in range(size)]
    durations = [[value // 10 for value in row] for row in distances]
    vehicles = [
        SolverVehicle(str(index), "VAN", 100_000, 10_000, 100, 100, 43_200, shift_window=(0, 43_200))
        for index in range(12)
    ]
    result = solve_vrp(SolverInput(nodes, vehicles, distances, durations, time_limit_s=30))
    assert result.status in {"OPTIMAL", "FEASIBLE", "TIME_LIMIT"}
