import environ

env = environ.FileAwareEnv()

# Whether the pipeline shards the original PDF for external job
# execution (issue #164). Kill switch for rollout; flipping it off
# skips sharding entirely without touching any stored shard set.
SHARDING_ENABLED = env.bool("SHARDING_ENABLED", default=True)

# Target byte size per shard. Byte-based rather than page-based because
# image density varies across reporters (#164 census: 1259-1878 KB per
# page). At the corpus median (~1.57 MB/page) 200 MB is ~127 pages per
# shard, so a 1300-page volume splits into ~11 shards that each bitonal
# in ~45s inside doctor's memory/CPU limits. Retuning this does NOT
# re-shard already-sharded volumes: a stored shard set is only
# recomputed when its source PDF changes (see sharding.ensure_shards).
SHARD_TARGET_BYTES = env.int("SHARD_TARGET_BYTES", default=200 * 1024**2)
