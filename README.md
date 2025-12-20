# GVex (Official Vexon GUI Runtime)

**GVex** is the official graphical runtime and event surface for the
[Vexon programming language](https://vexonlang.blogspot.com/).

GVex runs as a local process and exposes windows, widgets, canvas drawing,
images, and input events via an HTTP + JSON protocol, controlled by Vexon
programs using synchronous semantics.

---

## Features

- Window creation and control
- Widgets (label, button, input, image)
- Canvas drawing (rect, oval, line)
- Keyboard and mouse input
- Two-way event communication
- Local runtime (default: `localhost:5370`)
- Designed for games, tools, and interactive apps

---

## Architecture

```
Vexon Program  <---- HTTP / JSON ---->  GVex Runtime
(sync-first)                           (event-driven)
```

GVex is intentionally **decoupled** from the Vexon VM.
No embedding. No async required inside Vexon.

---

## Status

- **Spec:** GVex 0.8 (draft, stabilizing)
- **Vexon compatibility:** Vexon 0.4+
- **Runtime:** Python (reference implementation)

---

## Running the runtime

```bash
python runtime/vgr_server.py
```

Default address:

```
http://localhost:5370
```

---

## Demos

- `demos/demo.vx` — feature showcase

---

## License

Custom open-source license.
See [LICENSE](LICENSE).

