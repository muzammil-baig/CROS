create schema if not exists spatial;

create table if not exists spatial.hazard_area (
  hazard_id text primary key,
  hazard_type text not null,
  severity numeric(4,3) not null check (severity >= 0 and severity <= 1),
  water_level_m numeric(8,3),
  road_blocked boolean not null default false,
  active boolean not null default true,
  valid_from timestamptz not null default now(),
  valid_until timestamptz,
  geometry geometry(Geometry, 4326) not null,
  version bigint not null default 1,
  updated_at timestamptz not null default now()
);

create table if not exists spatial.road_segment (
  edge_id text primary key,
  from_node text not null,
  to_node text not null,
  road_class text not null,
  length_m numeric(12,2) not null check (length_m > 0),
  base_travel_seconds numeric(12,2) not null check (base_travel_seconds >= 0),
  geometry geometry(LineString, 4326) not null,
  graph_id text not null,
  graph_version bigint not null default 1,
  updated_at timestamptz not null default now()
);

create table if not exists spatial.route_snapshot (
  route_id uuid primary key default gen_random_uuid(),
  request_id text not null,
  graph_id text not null,
  graph_version bigint not null,
  hazard_version bigint not null default 0,
  origin geography(Point, 4326) not null,
  destination geography(Point, 4326) not null,
  route geometry(LineString, 4326) not null,
  status text not null check (status in ('OK', 'DEGRADED_ROUTE', 'NO_ROUTE', 'INVALIDATED')),
  eta_seconds numeric(12,2),
  computed_at timestamptz not null default now(),
  invalidated_at timestamptz,
  invalidation_reason text
);

create index if not exists hazard_area_geometry_gix on spatial.hazard_area using gist (geometry);
create index if not exists road_segment_geometry_gix on spatial.road_segment using gist (geometry);
create index if not exists route_snapshot_origin_gix on spatial.route_snapshot using gist (origin);
create index if not exists route_snapshot_destination_gix on spatial.route_snapshot using gist (destination);
create index if not exists route_snapshot_route_gix on spatial.route_snapshot using gist (route);
create index if not exists route_snapshot_request_idx on spatial.route_snapshot (request_id, computed_at desc);

alter table spatial.hazard_area enable row level security;
alter table spatial.road_segment enable row level security;
alter table spatial.route_snapshot enable row level security;

comment on schema spatial is 'Private CROS geospatial primitives; application services remain the authorization boundary.';
comment on column spatial.hazard_area.geometry is 'WGS84 hazard polygon/geometry used by ST_Intersects and ST_DWithin.';
comment on column spatial.route_snapshot.route is 'WGS84 computed route geometry with graph and hazard freshness metadata.';
