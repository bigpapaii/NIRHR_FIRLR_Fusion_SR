#!/usr/bin/env python3
import threading
import argparse
from typing import Optional, Tuple
from collections import deque

import numpy as np
import cv2

import gi
gi.require_version("Gst", "1.0")
gi.require_version("GstRtspServer", "1.0")
from gi.repository import Gst, GstRtspServer, GLib

Gst.init(None)


def now_ns() -> int:
    # monotonic time suitable for delta measurement
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
        # assumes packed BGR
        arr = np.frombuffer(mapinfo.data, dtype=np.uint8).reshape((h, w, 3))
        return arr.copy()
    finally:
        buf.unmap(mapinfo)


def have_ximgproc() -> bool:
    return hasattr(cv2, "ximgproc") and (
        hasattr(cv2.ximgproc, "guidedFilter") or hasattr(cv2.ximgproc, "jointBilateralFilter")
    )


def guided_refine(guide_gray_f: np.ndarray, src_f: np.ndarray, radius: int, eps: float) -> np.ndarray:
    """
    Edge-aware refinement (same size arrays), prefer guidedFilter if available.
    OpenCV guidedFilter signature documented in ximgproc. :contentReference[oaicite:1]{index=1}
    """
    if hasattr(cv2, "ximgproc") and hasattr(cv2.ximgproc, "guidedFilter"):
        return cv2.ximgproc.guidedFilter(guide_gray_f, src_f, radius, eps)
    # fallback: no-op
    return src_f


def joint_bilateral_refine(guide_u8: np.ndarray, src_u8: np.ndarray, d: int, sigmaColor: float, sigmaSpace: float) -> np.ndarray:
    """
    Joint bilateral filter (uses guide as joint image). Documented by OpenCV ximgproc. :contentReference[oaicite:2]{index=2}
    """
    if hasattr(cv2, "ximgproc") and hasattr(cv2.ximgproc, "jointBilateralFilter"):
        return cv2.ximgproc.jointBilateralFilter(guide_u8, src_u8, d, sigmaColor, sigmaSpace)
    return src_u8


def cmap_from_name(name: str) -> int:
    name = name.lower()
    table = {
        "jet": getattr(cv2, "COLORMAP_JET", 2),
        "turbo": getattr(cv2, "COLORMAP_TURBO", getattr(cv2, "COLORMAP_JET", 2)),
        "inferno": getattr(cv2, "COLORMAP_INFERNO", getattr(cv2, "COLORMAP_JET", 2)),
        "magma": getattr(cv2, "COLORMAP_MAGMA", getattr(cv2, "COLORMAP_JET", 2)),
        "plasma": getattr(cv2, "COLORMAP_PLASMA", getattr(cv2, "COLORMAP_JET", 2)),
        "viridis": getattr(cv2, "COLORMAP_VIRIDIS", getattr(cv2, "COLORMAP_JET", 2)),
    }
    return table.get(name, getattr(cv2, "COLORMAP_JET", 2))


class FusionState:
    def __init__(
        self,
        alpha: float,
        delta_ms: float,
        th_buf_len: int,
        p_low: float,
        p_high: float,
        colormap: int,
        blend_mode: str,
        edge_aware: bool,
        gf_radius: int,
        gf_eps: float,
    ):
        self.alpha = float(alpha)
        self.delta_ns = int(delta_ms * 1e6)
        self.th_buf = deque(maxlen=int(th_buf_len))

        self.p_low = float(p_low)
        self.p_high = float(p_high)
        self.colormap = colormap
        self.blend_mode = blend_mode.lower()
        self.edge_aware = bool(edge_aware)
        self.gf_radius = int(gf_radius)
        self.gf_eps = float(gf_eps)

        self.lock = threading.Lock()

        # latched thermal overlay prepared at output size (BGR uint8)
        self.held_overlay: Optional[np.ndarray] = None
        self.held_ts: Optional[int] = None

        # RTSP appsrc
        self.appsrc = None
        self.out_w: Optional[int] = None
        self.out_h: Optional[int] = None
        self.out_fps: Optional[int] = None

        # push thread
        self._pending: Optional[np.ndarray] = None
        self._pending_cv = threading.Condition()
        self._running = True
        self._push_thread = threading.Thread(target=self._push_loop, daemon=True)
        self._push_thread.start()

        if self.edge_aware and not have_ximgproc():
            print("[WARN] edge-aware enabled but cv2.ximgproc not found. Install opencv-contrib-python or disable --edge-aware.")

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
        print("[RTSP] appsrc cleared")

    def update_thermal(self, th_bgr: np.ndarray, ts_ns: int):
        with self.lock:
            self.th_buf.append((ts_ns, th_bgr))

    def _pick_nearest_thermal(self, gs_ts_ns: int) -> Optional[Tuple[int, np.ndarray, int]]:
        """
        Return (dt_ns, frame_ts, frame_bgr) for nearest thermal in buffer.
        """
        with self.lock:
            if not self.th_buf:
                return None
            best = None
            for tts, frame in self.th_buf:
                dt = abs(gs_ts_ns - tts)
                if best is None or dt < best[0]:
                    best = (dt, tts, frame)
            return best

    def _prepare_overlay(self, gs_bgr: np.ndarray, th_bgr: np.ndarray) -> np.ndarray:
        """
        Build thermal heatmap overlay at output size, optionally edge-aware guided by GS.
        """
        out_w, out_h = gs_bgr.shape[1], gs_bgr.shape[0]

        # thermal -> gray
        th_gray = cv2.cvtColor(th_bgr, cv2.COLOR_BGR2GRAY)

        # upsample to output size (use better interpolator than linear)
        th_up = cv2.resize(th_gray, (out_w, out_h), interpolation=cv2.INTER_CUBIC)

        # optional edge-aware refinement: guidedFilter on float32 guide/src
        if self.edge_aware and have_ximgproc():
            guide = cv2.cvtColor(gs_bgr, cv2.COLOR_BGR2GRAY).astype(np.float32) / 255.0
            src = th_up.astype(np.float32) / 255.0
            src_ref = guided_refine(guide, src, self.gf_radius, self.gf_eps)
            th_up = np.clip(src_ref * 255.0, 0, 255).astype(np.uint8)

        # robust normalize using percentiles
        lo = np.percentile(th_up, self.p_low)
        hi = np.percentile(th_up, self.p_high)
        if hi <= lo + 1e-6:
            hi = lo + 1.0
        th_norm = np.clip((th_up.astype(np.float32) - lo) / (hi - lo), 0.0, 1.0)
        th8 = (th_norm * 255.0).astype(np.uint8)

        # colorize
        th_color = cv2.applyColorMap(th8, self.colormap)
        return th_color

    def fuse_from_gs(self, gs_bgr: np.ndarray, gs_ts_ns: int):
        with self.lock:
            out_w, out_h = self.out_w, self.out_h

        if out_w is None or out_h is None:
            return  # no client yet

        # resize GS to output size (usually already)
        if gs_bgr.shape[1] != out_w or gs_bgr.shape[0] != out_h:
            gs_bgr = cv2.resize(gs_bgr, (out_w, out_h), interpolation=cv2.INTER_LINEAR)

        # update held overlay only if nearest thermal is within delta
        pick = self._pick_nearest_thermal(gs_ts_ns)
        if pick is not None:
            dt, th_ts, th_frame = pick
            if dt <= self.delta_ns:
                overlay = self._prepare_overlay(gs_bgr, th_frame)
                with self.lock:
                    self.held_overlay = overlay
                    self.held_ts = th_ts

        with self.lock:
            overlay = self.held_overlay
            held_ts = self.held_ts

        if overlay is None:
            fused = gs_bgr
        else:
            a = float(self.alpha)
            if self.blend_mode == "add":
                # additive overlay (often better for heatmaps)
                fused = cv2.addWeighted(gs_bgr, 1.0, overlay, a, 0.0)
            else:
                # standard mix
                fused = cv2.addWeighted(gs_bgr, 1.0 - a, overlay, a, 0.0)

            # debug: show dt on image (optional)
            if held_ts is not None:
                dt_ms = abs(gs_ts_ns - held_ts) / 1e6
                cv2.putText(
                    fused, f"dt={dt_ms:.1f}ms",
                    (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 1.0,
                    (255, 255, 255), 2
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

            # do-timestamp=true on appsrc => do not set pts/dts
            buf.duration = int(Gst.SECOND // out_fps)

            _ = appsrc.emit("push-buffer", buf)


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
        media.connect("unprepared", lambda _m: self.state.clear_appsrc())
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
    ap.add_argument("--delta-ms", type=float, default=80.0)
    ap.add_argument("--th-buf-len", type=int, default=16)
    ap.add_argument("--p-low", type=float, default=2.0)
    ap.add_argument("--p-high", type=float, default=98.0)
    ap.add_argument("--colormap", type=str, default="inferno")
    ap.add_argument("--blend-mode", type=str, default="add", choices=["add", "mix"])

    ap.add_argument("--edge-aware", action="store_true")
    ap.add_argument("--gf-radius", type=int, default=8)
    ap.add_argument("--gf-eps", type=float, default=1e-3)

    ap.add_argument("--bitrate-kbps", type=int, default=4000)
    args = ap.parse_args()

    state = FusionState(
        alpha=args.alpha,
        delta_ms=args.delta_ms,
        th_buf_len=args.th_buf_len,
        p_low=args.p_low,
        p_high=args.p_high,
        colormap=cmap_from_name(args.colormap),
        blend_mode=args.blend_mode,
        edge_aware=args.edge_aware,
        gf_radius=args.gf_radius,
        gf_eps=args.gf_eps,
    )

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
            state.update_thermal(frame, now_ns())
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
    print(f"ximgproc available: {have_ximgproc()} (guided/joint bilateral need OpenCV contrib)")

    loop = GLib.MainLoop()
    try:
        loop.run()
    finally:
        state.stop()
        th_pipe.set_state(Gst.State.NULL)
        gs_pipe.set_state(Gst.State.NULL)


if __name__ == "__main__":
    main()