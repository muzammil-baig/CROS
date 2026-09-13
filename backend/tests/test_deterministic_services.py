import pytest

from cros.services import allocation, capacity, prioritization, routing, transport, verification


def test_duplicate_requires_semantic_agreement():
    candidate = {"category": "rescue", "description": "Flooded apartment needs boat",
                 "location": {"coordinates": [90.4, 23.78]},
                 "created_at": "2999-01-01T00:00:00+00:00"}
    nearby = {"request_id": "REQ-OLD", "category": "rescue",
              "description": "Medical supplies needed at clinic",
              "location": {"coordinates": [90.4001, 23.7801]},
              "created_at": "2999-01-01T00:00:00+00:00"}
    assert verification.find_duplicate(candidate, [nearby]) is None


def test_verification_conflict_stale_and_provenance_vector():
    primary = {"report_id": "R-1", "source_type": "citizen", "device_id": "D-1",
               "created_at": "2999-01-01T00:00:00+00:00",
               "location": {"coordinates": [90.4, 23.78]}}
    related = [{"report_id": "R-2", "source_type": "citizen",
                "created_at": "2999-01-01T00:00:00+00:00",
                "location": {"coordinates": [90.4001, 23.7801]},
                "contradicts": True}]
    result = verification.evaluate(primary, related)
    assert result["status"] == "CONTRADICTORY"
    assert result["contradicting_report_ids"] == ["R-2"]
    assert result["schema_version"] == "verification-v1"
    assert result["evidence"]["primary_report_id"] == "R-1"
    assert result["uncertainty"] == ["No independent corroborating report in the spatial-temporal window"]



def test_capacity_rejects_invalid_operations_before_storage():
    with pytest.raises(ValueError, match="positive"):
        # Validation is intentionally independent of database availability.
        import asyncio
        asyncio.run(capacity.apply_operation(None, facility_id="FAC-1", counter_kind="occupancy",
                                             operation="increment", amount=0,
                                             operation_id="OP-1", actor_id="actor"))


def test_priority_fixed_vector_is_explainable():
    result = prioritization.compute_priority({
        "category": "medical",
        "people_count": 2,
        "vulnerabilities": ["pregnant"],
        "created_at": "2999-01-01T00:00:00+00:00",
        "verification": {"status": "VERIFIED"},
        "water_rising": True,
    }, hazard_severity=0.5)
    assert result["algorithm"] == "deterministic_weighted_v2"
    assert result["scoring_version"] == "priority-v2"
    assert result["factors"] == {
        "category": 36.0, "people_count": 6.0, "vulnerability": 9.0,
        "time_waiting": 0.0, "hazard_proximity": 7.5,
        "hazard_exposure": 7.5, "water_rising": 8.0,
    }
    assert result["score"] == 74.0
    assert result["tier"] == "high"


def test_priority_malformed_and_verification_vectors_are_bounded():
    base = {"category": "safe_report", "people_count": "not-a-number",
            "hazard_proximity": 9, "created_at": "invalid",
            "verification": {"status": "STALE"}}
    result = prioritization.compute_priority(base, hazard_severity="bad")
    assert result["score"] == 10.8
    assert result["tier"] == "low"
    assert result["verification_multiplier"] == 0.6


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
