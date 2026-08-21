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

# Ceiling on pages per shard, applied alongside the byte target (the
# shard count is the larger of the two demands). Needed because
# conversion cost is per *page* -- doctor rasterizes at the DPI we ask
# for, whatever the source's bytes/page -- while the byte target alone
# is blind to that. An already-bitonal 1300-page volume can sit well
# under 200 MB and would otherwise become a single ~5 minute request
# against a doctor pod with a 30s termination grace, with no
# parallelism and near-certain loss on the next rollout. 100 pages is
# ~25s at the measured 315 ms/page: chosen against the grace period,
# not against bytes. Going much lower buys nothing, since per-shard
# overhead (presign, download, upload, row, confirm) is already a few
# seconds. Like SHARD_TARGET_BYTES this is not part of the manifest
# fingerprint, so retuning it never re-shards existing volumes.
SHARD_MAX_PAGES = env.int("SHARD_MAX_PAGES", default=100)
