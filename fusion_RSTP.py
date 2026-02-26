#!/usr/bin/env python3
import threading
import argparse
from typing import Optional, Tuple

import numpy as np
import cv2

import gi
gi.require_version("Gst", "1.0")
gi.require_version("GstRtspServer", "1.0")
from gi.repository import Gst, GstRtspServer, GLib

Gst.init(None)


def now_ns() -> int:
    # Monotonic timestamp for interval measurement; deltas are meaningful.
    return int(Gst.util_get_timestamp())


def attach_bus_logger(pipeline: Gst.Pipeline, name: str):
    bus = pipeline.get_bus()
    bus.add_signal_watch()

    def on_msg(_bus, msg):
        t = msg.type
        if t == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            print(f"[{name}][ERROR] {err.message}")
            if dbg:
                print(f"[{name}][DEBUG] {dbg}")
        elif t == Gst.MessageType.WARNING:
            err, dbg = msg.parse_warning()
            print(f"[{name}][WARN] {err.message}")
            if dbg:
                print(f"[{name}][DEBUG] {dbg}")
        elif t == Gst.MessageType.EOS:
            print(f"[{name}] EOS")
    bus.connect("message", on_msg)


def sample_to_bgr(sample) -> Optional[np.ndarray]:
    buf = sample.get_buffer()
    caps = sample.get_caps()
    s = caps.get_structure(0)
    w = int(s.get_value("width"))
    h = int(s.get_value("height"))

    ok, mapinfo = buf.map(Gst.MapFlags.READ)
    if not ok:
        return None
    try:
        # Assumes tightly packed BGR (true for widths like 1280 and 160; stride==w*3)
        arr = np.frombuffer(mapinfo.data, dtype=np.uint8).reshape((h, w, 3))
        return arr.copy()
    finally:
        buf.unmap(mapinfo)


class FusionState:
    """
    latest_th_* : most recent thermal frame received (not necessarily simultaneous)
    held_th_*   : thermal frame latched only when some GS frame is within ±delta
    """
    def __init__(self, alpha: float, delta_ms: float):
        self.alpha = float(alpha)
        self.delta_ns = int(delta_ms * 1e6)

        self.lock = threading.Lock()

        self.latest_th_frame: Optional[np.ndarray] = None
        self.latest_th_ts: Optional[int] = None

        self.held_th_frame: Optional[np.ndarray] = None
        self.held_th_ts: Optional[int] = None

        # RTSP appsrc info (set after a client connects)
        self.appsrc = None
        self.out_w: Optional[int] = None
        self.out_h: Optional[int] = None
        self.out_fps: Optional[int] = None

        # Keep only newest pending fused frame
        self._pending: Optional[np.ndarray] = None
        self._pending_cv = threading.Condition()
        self._running = True
        self._push_thread = threading.Thread(target=self._push_loop, daemon=True)
        self._push_thread.start()

    def stop(self):
        with self._pending_cv:
            self._running = False
            self._pending_cv.notify_all()
        self._push_thread.join(timeout=2)

    def set_appsrc(self, appsrc, out_w: int, out_h: int, out_fps: int):
        with self.lock:
            self.appsrc = appsrc
            self.out_w = out_w
            self.out_h = out_h
            self.out_fps = out_fps
        print("[RTSP] appsrc ready")

    def clear_appsrc(self):
        with self.lock:
            self.appsrc = None
        print("[RTSP] appsrc cleared (client disconnected)")

    def update_thermal_latest(self, frame_bgr: np.ndarray, ts_ns: int):
        with self.lock:
            self.latest_th_frame = frame_bgr
            self.latest_th_ts = ts_ns

    def _maybe_refresh_held_thermal(self, gs_ts_ns: int):
        with self.lock:
            if self.latest_th_frame is None or self.latest_th_ts is None:
                return
            if abs(gs_ts_ns - self.latest_th_ts) <= self.delta_ns:
                self.held_th_frame = self.latest_th_frame
                self.held_th_ts = self.latest_th_ts

    def fuse_from_gs(self, gs_bgr: np.ndarray, gs_ts_ns: int):
        self._maybe_refresh_held_thermal(gs_ts_ns)

        with self.lock:
            held = self.held_th_frame
            held_ts = self.held_th_ts
            out_w, out_h = self.out_w, self.out_h

        if out_w is None or out_h is None:
            return  # no RTSP client yet

        # resize GS to output size
        if gs_bgr.shape[1] != out_w or gs_bgr.shape[0] != out_h:
            gs_bgr = cv2.resize(gs_bgr, (out_w, out_h), interpolation=cv2.INTER_LINEAR)

        if held is None:
            fused = gs_bgr
        else:
            if held.shape[1] != out_w or held.shape[0] != out_h:
                th2 = cv2.resize(held, (out_w, out_h), interpolation=cv2.INTER_LINEAR)
            else:
                th2 = held

            a = self.alpha
            fused = cv2.addWeighted(gs_bgr, 1.0 - a, th2, a, 0.0)

            # optional overlay: show stale held thermal
            if held_ts is not None and abs(gs_ts_ns - held_ts) > self.delta_ns:
                cv2.putText(
                    fused, "TH HOLD (STALE)",
                    (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 1.0,
                    (0, 0, 255), 2
                )

        with self._pending_cv:
            self._pending = fused
            self._pending_cv.notify()

    def _push_loop(self):
        while True:
            with self._pending_cv:
                while self._running and self._pending is None:
                    self._pending_cv.wait()
                if not self._running:
                    return
                frame_bgr = self._pending
                self._pending = None

            with self.lock:
                appsrc = self.appsrc
                out_fps = self.out_fps

            if appsrc is None or out_fps is None:
                continue

            data = frame_bgr.tobytes()
            buf = Gst.Buffer.new_allocate(None, len(data), None)
            buf.fill(0, data)

            # IMPORTANT: do NOT set buf.pts/dts here.
            # appsrc do-timestamp=true will timestamp buffers against the RTSP pipeline clock.
            buf.duration = int(Gst.SECOND // out_fps)

            ret = appsrc.emit("push-buffer", buf)
            if ret != Gst.FlowReturn.OK:
                # e.g. flushing/disconnected
                pass


class FusionFactory(GstRtspServer.RTSPMediaFactory):
    def __init__(self, state: FusionState, width: int, height: int, fps: int, bitrate_kbps: int):
        super().__init__()
        self.state = state
        self.width = width
        self.height = height
        self.fps = fps
        self.bitrate_kbps = bitrate_kbps

        launch = (
            f"( appsrc name=fsrc is-live=true format=time do-timestamp=true "
            f"block=false max-buffers=2 max-time=0 leaky-type=downstream "
            f"caps=video/x-raw,format=BGR,width={width},height={height},framerate={fps}/1 "
            f"! queue leaky=downstream max-size-buffers=2 "
            f"! videoconvert ! video/x-raw,format=I420 "
            f"! x264enc tune=zerolatency speed-preset=ultrafast bitrate={bitrate_kbps} key-int-max={fps} "
            f"! h264parse config-interval=1 "
            f"! rtph264pay name=pay0 pt=96 config-interval=1 )"
        )
        self.set_launch(launch)
        self.set_shared(True)

    def attach_media_callbacks(self):
        self.connect("media-configure", self._on_media_configure)

    def _on_media_configure(self, _factory, media):
        element = media.get_element()
        appsrc = element.get_by_name("fsrc")
        if appsrc is None:
            print("[RTSP][ERROR] failed to get appsrc")
            return

        # Clear on disconnect
        media.connect("unprepared", lambda _m: self.state.clear_appsrc())

        # In live mode, when appsrc does timestamping, keep min-latency at 0 if available.
        try:
            appsrc.set_property("min-latency", 0)
        except Exception:
            pass

        self.state.set_appsrc(appsrc, self.width, self.height, self.fps)


def make_appsinks(gs_w, gs_h, gs_fps, th_dev):
    gs_pipe = Gst.parse_launch(
        f"libcamerasrc "
        f"! video/x-raw,width={gs_w},height={gs_h},framerate={gs_fps}/1,format=NV12 "
        f"! queue leaky=downstream max-size-buffers=1 "
        f"! videoconvert ! video/x-raw,format=BGR "
        f"! appsink name=gssink emit-signals=true max-buffers=1 drop=true sync=false"
    )
    th_pipe = Gst.parse_launch(
        f"v4l2src device={th_dev} do-timestamp=true "
        f"! video/x-raw,format=RGB,width=160,height=120 "
        f"! queue leaky=downstream max-size-buffers=1 "
        f"! videoconvert ! video/x-raw,format=BGR "
        f"! appsink name=thsink emit-signals=true max-buffers=1 drop=true sync=false"
    )
    return gs_pipe, th_pipe


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8554)
    ap.add_argument("--path", type=str, default="/fusion")
    ap.add_argument("--gs-w", type=int, default=1280)
    ap.add_argument("--gs-h", type=int, default=720)
    ap.add_argument("--gs-fps", type=int, default=30)
    ap.add_argument("--th-dev", type=str, default="/dev/video42")
    ap.add_argument("--alpha", type=float, default=0.35)
    ap.add_argument("--delta-ms", type=float, default=50.0)
    ap.add_argument("--bitrate-kbps", type=int, default=4000)
    args = ap.parse_args()

    state = FusionState(args.alpha, args.delta_ms)

    # RTSP server
    server = GstRtspServer.RTSPServer()
    server.props.address = "0.0.0.0"
    server.props.service = str(args.port)
    mounts = server.get_mount_points()

    factory = FusionFactory(state, args.gs_w, args.gs_h, args.gs_fps, args.bitrate_kbps)
    factory.attach_media_callbacks()
    mounts.add_factory(args.path, factory)
    server.attach(None)

    # capture pipelines -> appsinks
    gs_pipe, th_pipe = make_appsinks(args.gs_w, args.gs_h, args.gs_fps, args.th_dev)
    attach_bus_logger(gs_pipe, "GS")
    attach_bus_logger(th_pipe, "TH")

    gssink = gs_pipe.get_by_name("gssink")
    thsink = th_pipe.get_by_name("thsink")

    def on_th_sample(sink):
        sample = sink.emit("pull-sample")
        frame = sample_to_bgr(sample)
        if frame is not None:
            state.update_thermal_latest(frame, now_ns())
        return Gst.FlowReturn.OK

    def on_gs_sample(sink):
        sample = sink.emit("pull-sample")
        frame = sample_to_bgr(sample)
        if frame is not None:
            state.fuse_from_gs(frame, now_ns())
        return Gst.FlowReturn.OK

    thsink.connect("new-sample", on_th_sample)
    gssink.connect("new-sample", on_gs_sample)

    th_pipe.set_state(Gst.State.PLAYING)
    gs_pipe.set_state(Gst.State.PLAYING)

    print(f"Fusion RTSP: rtsp://127.0.0.1:{args.port}{args.path}")
    loop = GLib.MainLoop()
    try:
        loop.run()
    finally:
        state.stop()
        th_pipe.set_state(Gst.State.NULL)
        gs_pipe.set_state(Gst.State.NULL)


if __name__ == "__main__":
    main()