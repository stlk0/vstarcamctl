# VStarcamCtl documentation

[Back to README](../README.md)

VStarcamCtl provides local camera control through both a command-line interface
and an async Python API. Start with the setup guide, then use the feature guide
that matches your task.

## Guides

- [Getting started](getting-started.md): install, configure, discover, and make
  the first read-only request.
- [CLI guide](cli.md): command groups, common reads, confirmed controls, and
  media tools.
- [Experimental features](experimental-features.md): guarded writes and
  model-dependent capabilities.
- [Python API](python-api.md): configuration, lifecycle, reads, writes, and
  media helpers.
- [Safety](safety.md): authorization, secret handling, one-shot writes, and
  physical recovery.

## Command categories

VStarcamCtl groups operations by safety and validation level:

- **Read-only** commands inspect state without changing camera configuration.
- **Confirmed** writes have a known effect but still require careful use.
- **Experimental** commands require `--experimental` because support may vary
  by camera or firmware.
- **Risky** commands also require `--confirm`.
- **Recovery-sensitive** commands require `--recovery-ready` after a physical
  recovery procedure has been checked.

The application enforces these gates in both the CLI and high-level API. A dry
run validates and masks a candidate command without connecting to the camera.

## Where to begin

For a new installation:

1. Follow [Getting started](getting-started.md).
2. Run only `discover`, `status`, and normalized status commands first.
3. Review [Safety](safety.md) before any write.
4. Use [Experimental features](experimental-features.md) only after confirming
   camera access and recovery options.
