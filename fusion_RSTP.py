#!/usr/bin/env python3
import time
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


def gst_time_ns_from_sample(sample) -> int:
    """
    Return timestamp in ns for alignment.
    Prefer GstBuffer PTS if available; otherwise fall back to monotonic time.
    """
    buf = sample.get_buffer()
    pts = buf.pts
    if pts is not None and pts != Gst.CLOCK_TIME_NONE:
        return int(pts)
    return time.monotonic_ns()


def sample_to_bgr(sample) -> Optional[np.ndarray]:
    buf = sample.get_buffer()
    caps = sample.get_caps()
    s = caps.get_structure(0)
    w = s.get_value("width")
    h = s.get_value("height")

    ok, mapinfo = buf.map(Gst.MapFlags.READ)
    if not ok:
        return None
    try:
        arr = np.frombuffer(mapinfo.data, dtype=np.uint8).reshape((h, w, 3))
        return arr.copy()
    finally:
        buf.unmap(mapinfo)


class FusionState:
    """
    latest_th_* : most recent thermal frame received (may not be "simultaneous")
    held_th_*   : thermal frame that has been accepted as "simultaneous" with some GS frame (|dt|<=delta)
                 This is the frame we keep/hold and blend into every GS frame until refreshed.
    """
    def __init__(self, alpha: float, delta_ms: float):
        self.alpha = float(alpha)
        self.delta_ns = int(delta_ms * 1e6)

        self.lock = threading.Lock()

        self.latest_th_frame: Optional[np.ndarray] = None
        self.latest_th_ts: Optional[int] = None

        self.held_th_frame: Optional[np.ndarray] = None
        self.held_th_ts: Optional[int] = None

        # appsrc is set when a client connects (media-configure)
        self.appsrc = None
        self.out_w: Optional[int] = None
        self.out_h: Optional[int] = None
        self.out_fps: Optional[int] = None

        # pushing thread: keep only newest pending fused frame to avoid latency buildup
        self._pending: Optional[Tuple[np.ndarray, int]] = None  # (frame_bgr, ts_ns)
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

    def clear_appsrc(self):
        with self.lock:
            self.appsrc = None

    def update_thermal_latest(self, frame_bgr: np.ndarray, ts_ns: int):
        with self.lock:
            self.latest_th_frame = frame_bgr
            self.latest_th_ts = ts_ns

    def _maybe_refresh_held_thermal(self, gs_ts_ns: int):
        """
        Update held thermal only when latest thermal is within ±delta of current GS.
        """
        with self.lock:
            if self.latest_th_frame is None or self.latest_th_ts is None:
                return
            if abs(gs_ts_ns - self.latest_th_ts) <= self.delta_ns:
                self.held_th_frame = self.latest_th_frame
                self.held_th_ts = self.latest_th_ts

    def fuse_from_gs(self, gs_bgr: np.ndarray, gs_ts_ns: int):
        """
        Called for every GS frame:
          - If latest thermal is within ±delta of this GS, refresh held thermal.
          - Blend GS with held thermal (or GS alone if no held thermal yet).
          - Enqueue fused frame for pushing to appsrc (drop older pending to keep low latency).
        """
        self._maybe_refresh_held_thermal(gs_ts_ns)

        with self.lock:
            held = self.held_th_frame
            held_ts = self.held_th_ts
            out_w, out_h = self.out_w, self.out_h

        # no client yet
        if out_w is None or out_h is None:
            return

        # resize GS -> output size
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

            # optional debug overlay: mark stale held thermal
            if held_ts is not None and abs(gs_ts_ns - held_ts) > self.delta_ns:
                cv2.putText(
                    fused, "TH HOLD (STALE)",
                    (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 1.0,
                    (0, 0, 255), 2
                )

        # enqueue newest fused frame; drop older pending to avoid latency accumulation
        with self._pending_cv:
            self._pending = (fused, gs_ts_ns)
            self._pending_cv.notify()

    def _push_loop(self):
        """
        Push fused frames into appsrc.
        appsrc queues internally; we keep only newest pending to avoid buildup.
        """
        while True:
            with self._pending_cv:
                while self._running and self._pending is None:
                    self._pending_cv.wait()
                if not self._running:
                    return
                frame_bgr, ts_ns = self._pending
                self._pending = None

            with self.lock:
                appsrc = self.appsrc
                out_fps = self.out_fps

            if appsrc is None or out_fps is None:
                continue

            data = frame_bgr.tobytes()
            buf = Gst.Buffer.new_allocate(None, len(data), None)
            buf.fill(0, data)

            # Provide timestamps explicitly (format=time).
            # We map gs_ts_ns (running-time ns) directly to PTS.
            buf.pts = ts_ns
            buf.dts = ts_ns
            buf.duration = int(Gst.SECOND // out_fps)

            # push-buffer can block if appsrc block=true; we set block=false & leaky-type=downstream in factory.
            ret = appsrc.emit("push-buffer", buf)
            if ret != Gst.FlowReturn.OK:
                # client might have disconnected or downstream not accepting
                pass


class FusionFactory(GstRtspServer.RTSPMediaFactory):
    def __init__(self, state: FusionState, width: int, height: int, fps: int, bitrate_kbps: int):
        super().__init__()
        self.state = state
        self.width = width
        self.height = height
        self.fps = fps
        self.bitrate_kbps = bitrate_kbps

        # Low-latency appsrc:
        # - block=false: don't stall producer thread
        # - max-buffers / max-time: keep small queue
        # - leaky-type=downstream: drop when full (avoid latency buildup)
        # These behaviors are documented for appsrc.
        launch = (
            f"( appsrc name=fsrc is-live=true format=time do-timestamp=false "
            f"block=false max-buffers=2 max-time=0 leaky-type=downstream "
            f"caps=video/x-raw,format=BGR,width={width},height={height},framerate={fps}/1 "
            f"! videoconvert ! video/x-raw,format=I420 "
            f"! x264enc tune=zerolatency speed-preset=ultrafast bitrate={bitrate_kbps} key-int-max={fps} "
            f"! h264parse config-interval=1 "
            f"! rtph264pay name=pay0 pt=96 config-interval=1 )"
        )
        self.set_launch(launch)
        self.set_shared(True)  # share one pipeline for all clients

    def attach_media_callbacks(self):
        # RTSPMediaFactory has media-configure signal for configuring per-media elements.
        self.connect("media-configure", self._on_media_configure)

    def _on_media_configure(self, factory, media):
        element = media.get_element()
        appsrc = element.get_by_name("fsrc")
        if appsrc is None:
            return

        # When client disconnects, clear appsrc
        media.connect("unprepared", lambda _m: self.state.clear_appsrc())

        self.state.set_appsrc(appsrc, self.width, self.height, self.fps)


def make_appsinks(gs_w, gs_h, gs_fps, th_dev):
    # appsink: new-sample emitted only when emit-signals=true.
    gs_pipe = Gst.parse_launch(
        f"libcamerasrc ! video/x-raw,width={gs_w},height={gs_h},framerate={gs_fps}/1,format=NV12 "
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
    gssink = gs_pipe.get_by_name("gssink")
    thsink = th_pipe.get_by_name("thsink")

    def on_th_sample(sink):
        sample = sink.emit("pull-sample")
        frame = sample_to_bgr(sample)
        if frame is not None:
            ts = gst_time_ns_from_sample(sample)
            state.update_thermal_latest(frame, ts)
        return Gst.FlowReturn.OK

    def on_gs_sample(sink):
        sample = sink.emit("pull-sample")
        frame = sample_to_bgr(sample)
        if frame is not None:
            ts = gst_time_ns_from_sample(sample)
            state.fuse_from_gs(frame, ts)
        return Gst.FlowReturn.OK

    thsink.connect("new-sample", on_th_sample)
    gssink.connect("new-sample", on_gs_sample)

    th_pipe.set_state(Gst.State.PLAYING)
    gs_pipe.set_state(Gst.State.PLAYING)

    print(f"Fusion RTSP: rtsp://<PI_IP>:{args.port}{args.path}")
    loop = GLib.MainLoop()
    try:
        loop.run()
    finally:
        state.stop()
        th_pipe.set_state(Gst.State.NULL)
        gs_pipe.set_state(Gst.State.NULL)


if __name__ == "__main__":
    main()