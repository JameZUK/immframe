# immframe

Picture-frame slideshow that pulls assets directly from an
[Immich](https://immich.app) server, with optional Home Assistant
integration via MQTT.

Derived from [picframe](https://github.com/helgeerbe/picframe) — the
pi3d-based renderer, mat compositor, and peripheral input code are
vendored from picframe; the filesystem scanner, SQLite cache, EXIF/IPTC
parser, and reverse-geocoder are dropped in favour of Immich's API.

Status: **interface stubs only.** See `src/immframe/` — no working
implementation yet.

## License

MIT. See LICENSE — picframe authors' attribution is preserved.
