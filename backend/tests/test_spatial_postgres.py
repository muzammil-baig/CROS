import asyncio
import json
import os

import asyncpg
import pytest


pytestmark = pytest.mark.skipif(
    not os.getenv("POSTGRES_URL_NON_POOLING"), reason="PostgreSQL integration is not configured"
)


async def _connect():
    return await asyncpg.connect(os.environ["POSTGRES_URL_NON_POOLING"])


def test_postgis_spatial_query_and_fixture_rollback():
    async def run():
        conn = await _connect()
        try:
            async with conn.transaction():
                await conn.execute(
                    """insert into spatial.hazard_area
                       (hazard_id, hazard_type, severity, geometry)
                       values ('TEST-SPATIAL-H', 'flood', 0.8,
                               ST_GeomFromText('POLYGON((90 23,91 23,91 24,90 24,90 23))',4326))"""
                )
                await conn.execute(
                    """insert into spatial.road_segment
                       (edge_id, from_node, to_node, road_class, length_m,
                        base_travel_seconds, geometry, graph_id)
                       values ('TEST-SPATIAL-R', 'A', 'B', 'primary', 100, 60,
                               ST_GeomFromText('LINESTRING(90.2 23.2,90.8 23.8)',4326),
                               'test-graph')"""
                )
                row = await conn.fetchrow(
                    """select ST_Intersects(h.geometry, r.geometry) as intersects,
                              ST_DWithin(h.geometry::geography, r.geometry::geography, 100000)
                              as within_100km
                       from spatial.hazard_area h join spatial.road_segment r on
                         h.hazard_id='TEST-SPATIAL-H' and r.edge_id='TEST-SPATIAL-R'"""
                )
                assert dict(row) == {"intersects": True, "within_100km": True}
                raise RuntimeError("rollback test")
        except RuntimeError:
            pass
        assert await conn.fetchval(
            "select count(*) from spatial.hazard_area where hazard_id='TEST-SPATIAL-H'"
        ) == 0
        await conn.close()

    asyncio.run(run())


def test_spatial_schema_has_gist_indexes():
    async def run():
        conn = await _connect()
        try:
            rows = await conn.fetch(
                """select indexname from pg_indexes where schemaname='spatial'
                   and indexdef ilike '%gist%' order by indexname"""
            )
            names = {row["indexname"] for row in rows}
            assert {
                "hazard_area_geometry_gix",
                "road_segment_geometry_gix",
                "route_snapshot_route_gix",
            } <= names
        finally:
            await conn.close()

    asyncio.run(run())
