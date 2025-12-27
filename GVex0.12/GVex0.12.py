#!/usr/bin/env python3
"""
GVex 0.12 — GVex FastAPI + Tkinter controller

Included upgrades over 0.11:
- Named widgets (aliases) and /alias_widget endpoint
- Layout managers (row, column, grid) with /set_layout
- Timers and animation helpers (/set_timer, /clear_timer, /animate)
- Data binding for entries, sliders, checkboxes
- Theme system (/set_theme) applied to widgets
- Plugin widget loader (gvex_plugins directory) for custom widgets
- Backwards-compatible with numeric IDs and existing endpoints

Usage:
- Run this script. It binds to http://localhost:5370 (or exits if port busy).
- Talk to it from Vexon via HTTP (fetch) at that address.

Design notes:
- Uses Tk's `after()` for timers and safe scheduling.
- Events are emitted via `events_queue` as before. When a named alias exists
  for a widget, events include the alias string in place of numeric id.
- Layout only affects widgets created without explicit x/y coordinates.

"""

from fastapi import FastAPI
from pydantic import BaseModel
import threading
import tkinter as tk
from tkinter import ttk
import uvicorn
from typing import Dict, Any, Optional, List, Tuple
import socket
import sys
from queue import Queue, Empty
from pathlib import Path
from tkinter import filedialog
import os
import importlib.util

# Optional PIL import for image widgets
try:
    from PIL import Image, ImageTk
    _HAS_PIL = True
except Exception:
    _HAS_PIL = False

app = FastAPI()

# --- State ---
ROOT: Optional[tk.Tk] = None
windows: Dict[int, tk.Toplevel] = {}
widgets: Dict[int, Any] = {}
# Map widget id -> window id for cleanup
widget_window: Dict[int, int] = {}
canvases: Dict[int, tk.Canvas] = {}
canvas_items: Dict[int, Tuple[tk.Canvas, int]] = {}
events_queue = Queue()
id_lock = threading.Lock()
ID = {"win": 1, "wid": 1, "can": 1, "itm": 1}

# Add timer id counter
ID["timer"] = 1

# --- Named widgets ---
widget_names: Dict[str, int] = {}

# --- Data bindings ---
widget_bindings: Dict[int, str] = {}

# --- Layout state ---
window_layouts: Dict[int, Dict[str, Any]] = {}

# --- Timers ---
timers: Dict[int, Dict[str, Any]] = {}

# --- Theme ---
theme: Dict[str, Any] = {"bg": None, "fg": None, "accent": None, "font": None}

# --- Plugins ---
plugin_widgets: Dict[str, Any] = {}

# --- Schemas ---
class WindowSchema(BaseModel):
    title: str = "Window"
    width: int = 400
    height: int = 300

class WidgetSchema(BaseModel):
    window_id: int
    type: str
    name: Optional[str] = None
    bind: Optional[str] = None
    text: Optional[str] = ""
    x: Optional[int] = None
    y: Optional[int] = None
    width: Optional[int] = None
    height: Optional[int] = None
    span: int = 1
    min_val: float = 0.0
    max_val: float = 100.0
    options: List[str] = []
    path: Optional[str] = None

class CanvasSchema(BaseModel):
    window_id: int
    width: int
    height: int
    x: int = 0
    y: int = 0

class CanvasItemSchema(BaseModel):
    canvas_id: int
    type: str
    x1: int
    y1: int
    x2: int
    y2: int
    fill: str = "white"

class MoveSchema(BaseModel):
    id: int
    dx: int
    dy: int
    is_canvas: bool = False

# --- Hostname / port setup ---
HOST = "localhost"
PORT = 5370


def _check_port_available(host: str, port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((host, port))
        s.close()
        return True
    except OSError:
        try:
            s.close()
        except Exception:
            pass
        return False

# --- UI helpers ---

def _schedule(func):
    if ROOT:
        try:
            ROOT.after(0, func)
        except Exception:
            # Root may be shutting down, run safely
            try:
                func()
            except Exception:
                pass
    else:
        # If ROOT is not ready, run immediately (rare at startup)
        try:
            func()
        except Exception:
            pass

# --- Plugin loader ---

def load_plugins(plugin_dir: str = "gvex_plugins"):
    p = Path(plugin_dir)
    if not p.exists() or not p.is_dir():
        return
    for path in p.glob("*.py"):
        try:
            spec = importlib.util.spec_from_file_location(path.stem, path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)  # type: ignore
            if hasattr(mod, "create"):
                plugin_widgets[path.stem] = mod.create
        except Exception:
            pass

# --- Internal helpers for input binding & cleanup ---

def _bind_input_to_window(win: tk.Toplevel, win_id: int):
    # Keyboard
    def on_key_down(e):
        events_queue.put({
            "type": "key",
            "key": e.keysym,
            "state": "down",
            "window_id": win_id
        })
    def on_key_up(e):
        events_queue.put({
            "type": "key",
            "key": e.keysym,
            "state": "up",
            "window_id": win_id
        })
    # Mouse
    def on_motion(e):
        events_queue.put({
            "type": "mouse",
            "event": "move",
            "x": e.x,
            "y": e.y,
            "window_id": win_id
        })
    def on_button_down(e):
        events_queue.put({
            "type": "mouse",
            "event": "down",
            "x": e.x,
            "y": e.y,
            "button": getattr(e, 'num', 1),
            "window_id": win_id
        })
    def on_button_up(e):
        events_queue.put({
            "type": "mouse",
            "event": "up",
            "x": e.x,
            "y": e.y,
            "button": getattr(e, 'num', 1),
            "window_id": win_id
        })
    def on_drag(e):
        events_queue.put({
            "type": "mouse",
            "event": "drag",
            "x": e.x,
            "y": e.y,
            "button": 1,
            "window_id": win_id
        })

    win.bind_all("<KeyPress>", on_key_down)
    win.bind_all("<KeyRelease>", on_key_up)
    win.bind("<Motion>", on_motion)
    win.bind("<ButtonPress>", on_button_down)
    win.bind("<ButtonRelease>", on_button_up)
    win.bind("<B1-Motion>", on_drag)
    try:
        win.focus_set()
    except Exception:
        pass


def _cleanup_window_resources(win_id: int):
    # Destroy widgets belonging to this window
    to_remove = [wid for wid, wwin in widget_window.items() if wwin == win_id]
    for wid in to_remove:
        w = widgets.pop(wid, None)
        try:
            if w:
                w.destroy()
        except Exception:
            pass
        widget_window.pop(wid, None)
        # Remove binding(s)
        widget_bindings.pop(wid, None)
        # Remove name aliases
        for name, _id in list(widget_names.items()):
            if _id == wid:
                widget_names.pop(name, None)

    # Destroy canvases owned by window
    to_remove_c = []
    for cid, c in list(canvases.items()):
        parent = c.master
        # Compare by top-level window
        try:
            top = parent.winfo_toplevel()
            # find window id for this top
            for wid, win in windows.items():
                if win == top:
                    to_remove_c.append(cid)
                    break
        except Exception:
            pass
    for cid in to_remove_c:
        try:
            canvases.pop(cid, None)
        except Exception:
            pass

# --- Named widget helpers ---

def _resolve_widget_id(data: Dict[str, Any]) -> Optional[int]:
    if not data:
        return None
    if "widget_id" in data and data.get("widget_id") is not None:
        try:
            return int(data.get("widget_id"))
        except Exception:
            pass
    if "widget_name" in data and data.get("widget_name"):
        return widget_names.get(data.get("widget_name"))
    # allow endpoints to accept `id` as well for legacy
    if "id" in data and data.get("id") is not None:
        try:
            return int(data.get("id"))
        except Exception:
            pass
    return None


def _widget_event_id(wid: int):
    # Prefer name alias if available
    for name, _id in widget_names.items():
        if _id == wid:
            return name
    return wid

# --- Layout helpers ---

def _resolve_layout_position(req: WidgetSchema) -> Tuple[int, int]:
    # If explicit coords provided, use them
    if req.x is not None or req.y is not None:
        return req.x or 0, req.y or 0

    layout = window_layouts.get(req.window_id)
    if not layout or layout.get("mode") == "none":
        # default fallbacks
        return 10, 10

    gap = layout.get("gap", 8)

    if layout.get("mode") == "row":
        x, y = layout["x"], layout["y"]
        # advance by width or a default
        advance = (req.width or layout.get("cell_w") or 100) + gap
        layout["x"] = layout["x"] + advance
        return x, y

    if layout.get("mode") == "column":
        x, y = layout["x"], layout["y"]
        advance = (req.height or layout.get("cell_h") or 30) + gap
        layout["y"] = layout["y"] + advance
        return x, y

    if layout.get("mode") == "grid":
        col = layout.get("col", 0)
        row = layout.get("row", 0)
        cell_w = layout.get("cell_w", 120)
        cell_h = layout.get("cell_h", 32)
        cols = layout.get("cols", 2)

        x = layout.get("x", 0) + col * (cell_w + gap)
        y = layout.get("y", 0) + row * (cell_h + gap)

        # advance column by span
        span = max(1, req.span or 1)
        layout["col"] = layout.get("col", 0) + span
        if layout["col"] >= cols:
            layout["col"] = 0
            layout["row"] = layout.get("row", 0) + 1

        return x, y

    return 10, 10

# --- Theme application ---

def _apply_theme_to_widget(w):
    if not w or not isinstance(w, (tk.Widget, ttk.Widget)):
        return
    try:
        if theme.get("bg") is not None:
            try:
                w.config(bg=theme.get("bg"))
            except Exception:
                pass
        if theme.get("fg") is not None:
            try:
                # many widgets use `fg` or `foreground`
                w.config(fg=theme.get("fg"))
            except Exception:
                try:
                    w.config(foreground=theme.get("fg"))
                except Exception:
                    pass
        if theme.get("font") is not None:
            try:
                w.config(font=tuple(theme.get("font")))
            except Exception:
                pass
    except Exception:
        pass


def _apply_theme():
    for w in widgets.values():
        _apply_theme_to_widget(w)

# --- Endpoints ---

@app.post("/create_window")
def create_window(req: WindowSchema):
    with id_lock:
        curr_id = ID["win"]
        ID["win"] += 1

    def _win_ui():
        win = tk.Toplevel(ROOT)
        win.title(req.title)
        win.geometry(f"{req.width}x{req.height}")
        windows[curr_id] = win

        # default layout for this window
        window_layouts[curr_id] = {"mode": "none", "x": 10, "y": 10, "row": 0, "col": 0, "gap": 8, "cell_w": 120, "cell_h": 32, "cols": 2}

        # Bind keyboard & mouse events for this window
        _bind_input_to_window(win, curr_id)

        # When window is closed by the user, inform Vexon and cleanup
        def _on_close(wid=curr_id):
            events_queue.put({"type": "window_close", "window_id": wid})
            try:
                _cleanup_window_resources(wid)
            except Exception:
                pass
            try:
                w = windows.pop(wid, None)
                if w: w.destroy()
            except Exception:
                pass

        win.protocol("WM_DELETE_WINDOW", _on_close)

    _schedule(_win_ui)
    return {"window_id": curr_id}


@app.post("/set_window_title")
def set_window_title(data: Dict[str, Any]):
    wid = int(data.get('window_id', -1))
    title = data.get('title', '')
    def _ui():
        win = windows.get(wid)
        if win:
            try:
                win.title(str(title))
            except Exception:
                pass
    _schedule(_ui)
    return {"status": "ok"}


@app.post("/resize_window")
def resize_window(data: Dict[str, Any]):
    wid = int(data.get('window_id', -1))
    w = int(data.get('width', 0))
    h = int(data.get('height', 0))
    def _ui():
        win = windows.get(wid)
        if win:
            try:
                win.geometry(f"{w}x{h}")
            except Exception:
                pass
    _schedule(_ui)
    return {"status": "ok"}


@app.post("/close_window")
def close_window(data: Dict[str, Any]):
    wid = int(data.get('window_id', -1))
    def _ui():
        try:
            _cleanup_window_resources(wid)
        except Exception:
            pass
        win = windows.pop(wid, None)
        try:
            if win:
                win.destroy()
        except Exception:
            pass
        # ensure Vexon knows the window closed
        events_queue.put({"type": "window_close", "window_id": wid})
    _schedule(_ui)
    return {"status": "ok"}


@app.post("/set_layout")
def set_layout(data: Dict[str, Any]):
    win = int(data.get("window_id"))
    mode = data.get("mode", "none")
    window_layouts[win] = {
        "mode": mode,
        "x": int(data.get("x", 10)),
        "y": int(data.get("y", 10)),
        "row": 0,
        "col": 0,
        "gap": int(data.get("gap", 8)),
        "cell_w": int(data.get("cell_w", 120)),
        "cell_h": int(data.get("cell_h", 32)),
        "cols": int(data.get("cols", 2))
    }
    return {"status": "ok"}


@app.post("/create_widget")
def create_widget(req: WidgetSchema):
    with id_lock:
        curr_id = ID["wid"]
        ID["wid"] += 1

    def _ui():
        parent = windows.get(req.window_id)
        if not parent:
            return
        t = req.type.lower()
        w = None

        # Helper to register widget after creation
        def _finalize_widget(wwidget):
            if not wwidget:
                return
            # placement using layout resolver
            x, y = _resolve_layout_position(req)
            try:
                wwidget.place(x=x, y=y, width=req.width, height=req.height)
            except Exception:
                try:
                    wwidget.place(x=x, y=y)
                except Exception:
                    pass
            widgets[curr_id] = wwidget
            widget_window[curr_id] = req.window_id
            # register bind if requested
            if req.bind:
                widget_bindings[curr_id] = req.bind
            # register name alias
            if req.name:
                widget_names[req.name] = curr_id
            # theme
            _apply_theme_to_widget(wwidget)

        if t == "button":
            def on_click(cid=curr_id):
                events_queue.put({"type": "click", "id": _widget_event_id(cid)})
            w = tk.Button(parent, text=req.text or "", command=on_click)

        elif t == "label":
            w = tk.Label(parent, text=req.text or "")

        elif t == "entry":
            # use StringVar for binding if requested
            if req.bind is not None:
                var = tk.StringVar()
                w = tk.Entry(parent, textvariable=var)
                def on_change(*_):
                    try:
                        events_queue.put({"type": "bind", "key": req.bind, "value": var.get()})
                    except Exception:
                        pass
                try:
                    var.trace_add("write", on_change)
                except Exception:
                    try:
                        var.trace("w", on_change)
                    except Exception:
                        pass
                w._var = var
            else:
                w = tk.Entry(parent)

            def on_submit(e, cid=curr_id, widget=w):
                try:
                    events_queue.put({"type": "submit", "id": _widget_event_id(cid), "value": widget.get()})
                except Exception:
                    events_queue.put({"type": "submit", "id": _widget_event_id(cid), "value": ""})
            w.bind("<Return>", on_submit)

        elif t == "checkbox":
            var = tk.IntVar()
            w = tk.Checkbutton(parent, text=req.text or "", variable=var,
                               command=lambda cid=curr_id, v=var: events_queue.put({"type": "toggle", "id": _widget_event_id(cid), "value": v.get()}))
            w._v = var
            # binding
            if req.bind is not None:
                def on_cb(*_):
                    try:
                        events_queue.put({"type": "bind", "key": req.bind, "value": w._v.get()})
                    except Exception:
                        pass
                try:
                    var.trace_add("write", lambda *_: on_cb())
                except Exception:
                    pass

        elif t == "slider":
            # use DoubleVar for binding
            var = tk.DoubleVar()
            try:
                w = tk.Scale(parent, from_=req.min_val, to=req.max_val, orient=tk.HORIZONTAL, variable=var,
                             command=lambda v, cid=curr_id: events_queue.put({"type": "change", "id": _widget_event_id(cid), "value": float(v)}))
                w._v = var
            except Exception:
                w = tk.Scale(parent, from_=req.min_val, to=req.max_val, orient=tk.HORIZONTAL,
                             command=lambda v, cid=curr_id: events_queue.put({"type": "change", "id": _widget_event_id(cid), "value": float(v)}))

            if req.bind is not None:
                try:
                    var.trace_add("write", lambda *_: events_queue.put({"type": "bind", "key": req.bind, "value": var.get()}))
                except Exception:
                    pass

        elif t == "textarea":
            w = tk.Text(parent)
            if req.text:
                try:
                    w.insert("1.0", req.text)
                except Exception:
                    pass

        elif t == "progressbar":
            w = ttk.Progressbar(parent, orient=tk.HORIZONTAL, length=req.width or 100, mode='determinate')
            try:
                w['maximum'] = req.max_val
                w['value'] = req.min_val
            except Exception:
                pass

        elif t == "listbox":
            w = tk.Listbox(parent)
            for item in req.options:
                w.insert(tk.END, item)
            def _on_select(e, cid=curr_id, lb=None):
                try:
                    if lb is None:
                        lb = e.widget
                    events_queue.put({"type": "select", "id": _widget_event_id(cid), "index": lb.curselection()})
                except Exception:
                    events_queue.put({"type": "select", "id": _widget_event_id(cid), "index": []})
            w.bind("<<ListboxSelect>>", lambda e, cid=curr_id: _on_select(e, cid, w))

        elif t == "image":
            # PIL-backed image label
            if not _HAS_PIL:
                # If PIL isn't available, fallback to a label with text
                w = tk.Label(parent, text=req.text or "[PIL missing]")
            else:
                try:
                    img_path = req.path or req.text or ""
                    if img_path and os.path.exists(img_path):
                        img = Image.open(img_path)
                        tk_img = ImageTk.PhotoImage(img)
                        w = tk.Label(parent, image=tk_img)
                        # Keep reference to avoid GC
                        w._img = tk_img
                    else:
                        # Support missing path gracefully
                        w = tk.Label(parent, text=req.text or "[image not found]")
                except Exception:
                    w = tk.Label(parent, text=req.text or "[image error]")

        elif t == "canvas":
            # create a canvas widget inline (non-managed canvas creation)
            try:
                w = tk.Canvas(parent, width=req.width or 200, height=req.height or 150, bg="black")
            except Exception:
                w = tk.Canvas(parent)

        elif t in plugin_widgets:
            try:
                # plugin create(parent, req, events_queue) -> widget
                w = plugin_widgets[t](parent, req, events_queue)
            except Exception:
                w = tk.Label(parent, text=f"[plugin error: {t}]")

        else:
            w = tk.Label(parent, text=req.text or "")

        # Finalize registration
        _finalize_widget(w)

    _schedule(_ui)
    return {"widget_id": curr_id}


@app.post("/create_canvas")
def create_canvas(req: CanvasSchema):
    with id_lock:
        curr_id = ID["can"]
        ID["can"] += 1
    def _ui():
        parent = windows.get(req.window_id)
        if parent:
            c = tk.Canvas(parent, width=req.width, height=req.height, bg="black")
            c.place(x=req.x, y=req.y)
            canvases[curr_id] = c
    _schedule(_ui)
    return {"canvas_id": curr_id}


@app.post("/create_canvas_item")
def create_canvas_item(req: CanvasItemSchema):
    with id_lock:
        curr_id = ID["itm"]
        ID["itm"] += 1
    def _ui():
        c = canvases.get(req.canvas_id)
        if not c: return
        try:
            if req.type == "rect":
                item = c.create_rectangle(req.x1, req.y1, req.x2, req.y2, fill=req.fill)
            elif req.type == "oval":
                item = c.create_oval(req.x1, req.y1, req.x2, req.y2, fill=req.fill)
            elif req.type == "line":
                item = c.create_line(req.x1, req.y1, req.x2, req.y2, fill=req.fill)
            else:
                item = c.create_rectangle(req.x1, req.y1, req.x2, req.y2, fill=req.fill)
            canvas_items[curr_id] = (c, item)
        except Exception:
            pass
    _schedule(_ui)
    return {"item_id": curr_id}


@app.post("/move")
def move_object(req: MoveSchema):
    def _ui():
        if req.is_canvas:
            tup = canvas_items.get(req.id)
            if tup:
                c, item = tup
                try:
                    c.move(item, req.dx, req.dy)
                except Exception:
                    pass
        else:
            w = widgets.get(req.id)
            if w:
                try:
                    info = w.place_info()
                    x = int(float(info.get('x', 0)))
                    y = int(float(info.get('y', 0)))
                    w.place(x=x+req.dx, y=y+req.dy)
                except Exception:
                    pass
    _schedule(_ui)
    return {"status": "ok"}


@app.get("/get_event")
def get_event():
    try:
        return events_queue.get(timeout=0.1)
    except Empty:
        return {"type": "idle"}


@app.post("/set_widget_value")
def set_widget_value(data: Dict[str, Any]):
    wid = _resolve_widget_id(data)
    if wid is None:
        return {"status": "error", "error": "Unknown widget"}
    val = data.get('value', 0)
    try:
        valf = float(val)
    except Exception:
        valf = val
    def _ui():
        w = widgets.get(wid)
        if isinstance(w, (tk.Scale, ttk.Progressbar)):
            try:
                w['value'] = valf
            except Exception:
                pass
        elif isinstance(w, tk.Checkbutton):
            try:
                w._v.set(int(valf))
            except Exception:
                pass
        elif isinstance(w, tk.Entry) and hasattr(w, '_var'):
            try:
                w._var.set(str(val))
            except Exception:
                pass
    _schedule(_ui)
    return {"status": "ok"}


@app.post("/set_widget_text")
def set_widget_text(data: Dict[str, Any]):
    wid = _resolve_widget_id(data)
    if wid is None:
        return {"status": "error", "error": "Unknown widget"}
    text = data.get('text', '')
    def _ui():
        w = widgets.get(wid)
        if not w: return
        try:
            if isinstance(w, tk.Entry):
                w.delete(0, tk.END)
                w.insert(0, str(text))
            elif isinstance(w, (tk.Label, tk.Button, tk.Checkbutton)):
                w.config(text=str(text))
            elif isinstance(w, tk.Text):
                w.delete("1.0", tk.END)
                w.insert("1.0", str(text))
        except Exception:
            pass
    _schedule(_ui)
    return {"status": "ok"}


@app.post("/get_widget_data")
def get_widget_data(data: Dict[str, Any]):
    wid = _resolve_widget_id(data)
    if wid is None:
        return {"value": None}
    w = widgets.get(wid)
    if not w: return {"value": None}
    try:
        if isinstance(w, tk.Entry): return {"value": w.get()}
        if isinstance(w, tk.Text): return {"value": w.get("1.0", tk.END).strip()}
        if isinstance(w, tk.Scale): return {"value": w.get()}
        if isinstance(w, tk.Checkbutton): return {"value": w._v.get()}
        if isinstance(w, tk.Listbox): return {"value": list(w.get(0, tk.END))}
    except Exception:
        pass
    return {"value": None}


@app.post("/delete_widget")
def delete_widget(data: Dict[str, Any]):
    wid = _resolve_widget_id(data)
    if wid is None:
        return {"status": "error", "error": "Unknown widget"}
    def _ui():
        w = widgets.pop(wid, None)
        try:
            if w: w.destroy()
        except Exception:
            pass
        try:
            widget_window.pop(wid, None)
        except Exception:
            pass
        # Remove name aliases
        for name, _id in list(widget_names.items()):
            if _id == wid:
                widget_names.pop(name, None)
        # Remove bindings
        widget_bindings.pop(wid, None)
    _schedule(_ui)
    return {"status": "ok"}


@app.post("/file_open_dialog")
def file_open_dialog():
    path = filedialog.askopenfilename(filetypes=[("Text Files", "*.txt"), ("All Files", "*.*")])
    return {"path": path}


@app.post("/file_save_dialog")
def file_save_dialog():
    path = filedialog.asksaveasfilename(defaultextension=".txt", filetypes=[("Text Files", "*.txt")])
    return {"path": path}


@app.post("/read_file")
def read_file_endpoint(data: Dict[str, Any]):
    path = data.get("path")
    if not path or not os.path.exists(path):
        return {"content": "", "error": "File not found"}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return {"content": f.read()}
    except Exception as e:
        return {"content": "", "error": str(e)}


@app.post("/write_file")
def write_file_endpoint(data: Dict[str, Any]):
    path = data.get("path")
    content = data.get("content", "")
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return {"status": "success"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@app.post("/post_event")
def post_event(data: Dict[str, Any]):
    # Allows external clients to inject arbitrary events into the Vexon event queue.
    try:
        events_queue.put(data)
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


# --- Named widget simple endpoint ---
@app.post('/alias_widget')
def alias_widget(data: Dict[str, Any]):
    name = data.get('name')
    wid = data.get('widget_id') or data.get('id')
    try:
        wid = int(wid)
    except Exception:
        return {"status": "error", "error": "invalid id"}
    widget_names[name] = wid
    return {"status": "ok"}

# --- Timers / Animation ---
@app.post('/set_timer')
def set_timer(data: Dict[str, Any]):
    with id_lock:
        tid = ID['timer']
        ID['timer'] += 1
    ms = int(data.get('ms', 1000))
    repeat = bool(data.get('repeat', False))
    event = data.get('event', {'type': 'timer', 'id': tid})

    def _tick():
        if tid not in timers:
            return
        events_queue.put(event)
        if repeat and tid in timers:
            try:
                ROOT.after(ms, _tick)
            except Exception:
                pass
        else:
            timers.pop(tid, None)

    timers[tid] = {'ms': ms, 'repeat': repeat, 'event': event}
    _schedule(lambda: ROOT.after(ms, _tick))
    return {'timer_id': tid}


@app.post('/clear_timer')
def clear_timer(data: Dict[str, Any]):
    tid = int(data.get('timer_id'))
    timers.pop(tid, None)
    return {'status': 'ok'}


@app.post('/animate')
def animate(data: Dict[str, Any]):
    # High-level animation helper (linear interpolation)
    # Accept widget_id or widget_name
    wid = _resolve_widget_id(data)
    if wid is None:
        return {"status": "error", "error": "Unknown widget"}
    dx = float(data.get('dx', 0))
    dy = float(data.get('dy', 0))
    steps = int(data.get('steps', 30))
    ms = int(data.get('ms', 16))
    step_dx = dx / steps
    step_dy = dy / steps
    count = {'i': 0}

    def _step():
        if count['i'] >= steps:
            return
        w = widgets.get(wid)
        if w:
            try:
                info = w.place_info()
                x = int(float(info.get('x', 0)))
                y = int(float(info.get('y', 0)))
                w.place(x=x + step_dx, y=y + step_dy)
            except Exception:
                pass
        count['i'] += 1
        ROOT.after(ms, _step)

    _schedule(_step)
    return {"status": "ok"}


# --- Theme endpoints ---
@app.post('/set_theme')
def set_theme(data: Dict[str, Any]):
    # Accepts bg, fg, accent, font
    for k in ['bg', 'fg', 'accent', 'font']:
        if k in data:
            theme[k] = data.get(k)
    _schedule(_apply_theme)
    return {'status': 'ok'}


# --- Main ---
if __name__ == "__main__":
    # Fail fast: check that we can bind to desired host:port
    if not _check_port_available(HOST, PORT):
        print(f"[GVex] ERROR: Cannot bind to {HOST}:{PORT}. Is another process using the port?")
        sys.exit(1)

    # Load plugins if present
    load_plugins()

    def run_api():
        # bind to localhost:5370 (must succeed because we checked above)
        uvicorn.run(app, host=HOST, port=PORT, log_level="error")

    t = threading.Thread(target=run_api, daemon=True)
    t.start()

    # Tkinter main window
    ROOT = tk.Tk()
    ROOT.title("GVex Controller")
    lbl = tk.Label(ROOT, text=f"GVex Engine Active — http://{HOST}:{PORT}", fg="blue", font=("Arial", 12))
    lbl.pack(padx=20, pady=20)

    try:
        ROOT.mainloop()
    except KeyboardInterrupt:
        try:
            ROOT.destroy()
        except Exception:
            pass
        sys.exit(0)
