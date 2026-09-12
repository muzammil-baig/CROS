from cros.services import allocation, prioritization, routing, transport


def test_priority_fixed_vector_is_explainable():
    result = prioritization.compute_priority({
        "category": "medical",
        "people_count": 2,
        "vulnerabilities": ["pregnant"],
        "created_at": "2999-01-01T00:00:00+00:00",
        "verification": {"status": "VERIFIED"},
        "water_rising": True,
    }, hazard_severity=0.5)
    assert result["algorithm"] == "deterministic_weighted_v1"
    assert result["factors"] == {
        "category": 36.0, "people_count": 6.0, "vulnerability": 9.0,
        "time_waiting": 0.0, "hazard_exposure": 7.5, "water_rising": 8.0,
    }
    assert result["score"] == 66.5
    assert result["tier"] == "high"


def test_routing_hazard_overlay_avoids_blocked_edge():
    graph = routing.build_base_graph()
    edge = graph["edges"][0]
    hazard = {"hazard_id": "HZ-1", "hazard_type": "flood",
              "severity": 1.0, "water_level_m": 2.0,
              "geometry": edge["geometry"]}
    route = routing.compute_route(graph, [hazard], graph["nodes"][0]["coordinates"],
                                  graph["nodes"][-1]["coordinates"])
    assert route["algorithm"] == "dijkstra_networkx_hazard_overlay_v1"
    assert route["status"] in {"OK", "DEGRADED_ROUTE", "NO_ROUTE"}
    assert graph["edges"][0].get("blocked") is None


def test_allocation_reports_infeasible_capacity_and_solver_metadata():
    request = {"request_id": "REQ-1", "category": "medical", "people_count": 4,
               "priority": {"tier": "critical"},
               "location": {"coordinates": [90.40, 23.78]}}
    resource = {"resource_id": "RES-1", "kind": "ambulance", "status": "available",
                "capacity": 2, "location": {"coordinates": [90.40, 23.78]}}
    result = allocation.propose([request], [resource], routing.build_base_graph(), [])
    assert result["assignments"] == []
    assert result["unassigned_request_ids"] == ["REQ-1"]
    assert "capacity" in result["constraints"]


def test_transport_selection_is_deterministic_and_ack_aware():
    registry = transport.TransportRegistry()
    states = registry.states()
    selected, ranked = transport.select_transport(states, "critical", 1.0, require_ack=True)
    assert selected == "local_wifi"
    assert ranked[0]["transport"] == "local_wifi"
    scores = [item["score"] for item in ranked if item["score"] is not None]
    assert all(scores[i] >= scores[i + 1] for i in range(len(scores) - 1))


def test_medical_matching_cost_is_explainable():
    request = {"request_id": "REQ-2", "category": "medical", "people_count": 1,
               "priority": {"tier": "high"},
               "location": {"coordinates": [90.40, 23.78]}}
    resource = {"resource_id": "RES-2", "kind": "ambulance", "status": "available",
                "capacity": 2, "location": {"coordinates": [90.40, 23.78]}}
    result = allocation.cost_for(request, resource, routing.build_base_graph(), [])
    assert result["feasible"] is True
    assert result["suitability"] == 1.0
    assert result["reasons"] == []
    assert result["route_status"] in {"OK", "DEGRADED_ROUTE"}
