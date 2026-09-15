alter table if exists events.event_log
  add column if not exists origin_device_source text;

comment on column events.event_log.origin_device_source is
  'Lossless source device identifier from the signed envelope; origin_device_id remains the canonical FK mapping.';

create index if not exists event_log_origin_device_source_idx
  on events.event_log (origin_device_source);
