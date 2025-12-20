#!/usr/bin/env python3
"""
GVex 0.8 — GVex FastAPI + Tkinter controller (final patched)

Features added in 0.8:
- Networking fixed to localhost:5370 (fails fast on bind)
- /set_window_title, /resize_window, /close_window
- Keyboard events (key down / key up)
- Mouse events (move, down, up, drag)
- Image widget (PIL-backed) with ImageTk
- Improved cleanup: widget <-> window tracking
- /post_event endpoint for arbitrary event injection
- Emits window-close events when windows are destroyed

Usage:
- Run this script. It binds to http://localhost:5370 or exits with error if port is busy.
- Talk to it from Vexon via HTTP (fetch) at that address.
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

# --- Schemas ---
class WindowSchema(BaseModel):
    title: str = "Window"
    width: int = 400
    height: int = 300

class WidgetSchema(BaseModel):
    window_id: int
    type: str
    text: Optional[str] = ""
    x: int = 10
    y: int = 10
    width: Optional[int] = None
    height: Optional[int] = None
    # New fields for advanced widgets
    min_val: float = 0.0
    max_val: float = 100.0
    options: List[str] = []
    # For images
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
        ROOT.after(0, func)
    else:
        # If ROOT is not ready, run immediately (rare at startup)
        try:
            func()
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
    # Common drag for left button
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

        if t == "button":
            def on_click(cid=curr_id):
                events_queue.put({"type": "click", "id": cid})
            w = tk.Button(parent, text=req.text or "", command=on_click)
        elif t == "label":
            w = tk.Label(parent, text=req.text or "")
        elif t == "entry":
            w = tk.Entry(parent)
            def on_submit(e, cid=curr_id, widget=w):
                try:
                    events_queue.put({"type": "submit", "id": cid, "value": widget.get()})
                except Exception:
                    events_queue.put({"type": "submit", "id": cid, "value": ""})
            w.bind("<Return>", on_submit)
        elif t == "checkbox":
            var = tk.IntVar()
            w = tk.Checkbutton(parent, text=req.text or "", variable=var,
                               command=lambda: events_queue.put({"type": "toggle", "id": curr_id, "value": var.get()}))
            w._v = var
        elif t == "slider":
            w = tk.Scale(parent, from_=req.min_val, to=req.max_val, orient=tk.HORIZONTAL,
                         command=lambda v: events_queue.put({"type": "change", "id": curr_id, "value": float(v)}))
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
                    events_queue.put({"type": "select", "id": cid, "index": lb.curselection()})
                except Exception:
                    events_queue.put({"type": "select", "id": cid, "index": []})
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
        else:
            w = tk.Label(parent, text=req.text or "")

        if w:
            try:
                w.place(x=req.x, y=req.y, width=req.width, height=req.height)
            except Exception:
                try:
                    w.place(x=req.x, y=req.y)
                except Exception:
                    pass
            widgets[curr_id] = w
            widget_window[curr_id] = req.window_id

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
    wid = int(data.get('widget_id'))
    val = float(data.get('value', 0))
    def _ui():
        w = widgets.get(wid)
        if isinstance(w, (tk.Scale, ttk.Progressbar)):
            try:
                w['value'] = val
            except Exception:
                pass
        elif isinstance(w, tk.Checkbutton):
            try:
                w._v.set(int(val))
            except Exception:
                pass
    _schedule(_ui)
    return {"status": "ok"}


@app.post("/set_widget_text")
def set_widget_text(data: Dict[str, Any]):
    wid = int(data.get('widget_id'))
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
    wid = int(data.get('widget_id'))
    w = widgets.get(wid)
    if not w: return {"value": None}
    try:
        if isinstance(w, tk.Entry): return {"value": w.get()}
        if isinstance(w, tk.Text): return {"value": w.get("1.0", tk.END).strip()}
        if isinstance(w, tk.Scale): return {"value": w.get()}
        if isinstance(w, tk.Checkbutton): return {"value": w._v.get()}
    except Exception:
        pass
    return {"value": None}


@app.post("/delete_widget")
def delete_widget(data: Dict[str, Any]):
    wid = int(data.get('widget_id'))
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


# --- Main ---
if __name__ == "__main__":
    # Fail fast: check that we can bind to desired host:port
    if not _check_port_available(HOST, PORT):
        print(f"[GVex] ERROR: Cannot bind to {HOST}:{PORT}. Is another process using the port?")
        sys.exit(1)

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
