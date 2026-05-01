// Mobile-web (PWA) frame streaming client.
//
// The server's wire protocol expects an alternating sequence of:
//   1) JSON header  {"type":"frame","idx":N,"ts":<ms>,"pose":?, "intrinsics":?}
//   2) raw binary JPEG bytes
// per captured frame, plus the lifecycle messages (session/heartbeat/
// finalize). This client wraps the WebSocket + a <video> element +
// an offscreen canvas so the page-side code only has to start/stop
// + render the live preview.

import { wsUrl } from "./api";

export interface StreamCallbacks {
  onAck?: (ack: { received: number; dropped: number }) => void;
  onLimit?: (info: { reason: string; cap: number }) => void;
  onQueued?: (sceneId: string) => void;
  onError?: (err: Error) => void;
  onClose?: () => void;
}

export interface StreamOptions {
  captureId: string;
  pairToken: string;
  fps?: number;
  jpegQuality?: number;
}

export class CaptureStreamer {
  private ws: WebSocket | null = null;
  private timer: number | null = null;
  private idx = 0;
  private canvas = document.createElement("canvas");
  private ctx = this.canvas.getContext("2d", { alpha: false })!;

  constructor(
    private readonly video: HTMLVideoElement,
    private readonly opts: StreamOptions,
    private readonly cbs: StreamCallbacks = {},
  ) {}

  async start(stream: MediaStream): Promise<void> {
    this.video.srcObject = stream;
    await this.video.play();

    const url = new URL(
      wsUrl(`/api/captures/${this.opts.captureId}/stream`),
    );
    url.searchParams.set("token", this.opts.pairToken);
    this.ws = new WebSocket(url.toString());
    this.ws.binaryType = "arraybuffer";

    await new Promise<void>((resolve, reject) => {
      if (!this.ws) return reject(new Error("ws missing"));
      this.ws.onopen = () => resolve();
      this.ws.onerror = () => reject(new Error("websocket error"));
    });

    this.ws.onmessage = (e) => this.handleMessage(e);
    this.ws.onclose = () => this.cbs.onClose?.();

    const intrinsics = this.estimateIntrinsics(stream);
    this.send({
      type: "session",
      device: { userAgent: navigator.userAgent },
      intrinsics,
      has_pose: false, // PWA path can't supply pose; server runs SfM
    });

    const fps = this.opts.fps ?? 5;
    const interval = Math.max(50, Math.floor(1000 / fps));
    this.timer = window.setInterval(() => {
      void this.captureFrame();
    }, interval);
  }

  async stop(reason: "user" | "timeout" = "user"): Promise<void> {
    if (this.timer) {
      window.clearInterval(this.timer);
      this.timer = null;
    }
    this.send({ type: "finalize", reason });
    setTimeout(() => this.ws?.close(), 5000);
  }

  private async captureFrame(): Promise<void> {
    const v = this.video;
    if (v.readyState < 2 || !this.ws || this.ws.readyState !== WebSocket.OPEN) {
      return;
    }
    const w = v.videoWidth;
    const h = v.videoHeight;
    if (!w || !h) return;
    if (this.canvas.width !== w || this.canvas.height !== h) {
      this.canvas.width = w;
      this.canvas.height = h;
    }
    this.ctx.drawImage(v, 0, 0, w, h);
    const blob = await new Promise<Blob | null>((r) =>
      this.canvas.toBlob(r, "image/jpeg", this.opts.jpegQuality ?? 0.85),
    );
    if (!blob) return;

    const idx = this.idx++;
    this.send({
      type: "frame",
      idx,
      ts: Date.now(),
      pose: null,
      intrinsics: null,
    });
    const buf = await blob.arrayBuffer();
    this.ws.send(buf);
  }

  private handleMessage(e: MessageEvent): void {
    if (typeof e.data !== "string") return;
    try {
      const msg = JSON.parse(e.data);
      if (msg.type === "ack") {
        this.cbs.onAck?.({
          received: msg.frames_received,
          dropped: msg.frames_dropped,
        });
      } else if (msg.type === "limit") {
        this.cbs.onLimit?.({ reason: msg.reason, cap: msg.cap });
      } else if (msg.type === "queued") {
        this.cbs.onQueued?.(msg.scene_id);
      }
    } catch (err) {
      this.cbs.onError?.(err as Error);
    }
  }

  private estimateIntrinsics(stream: MediaStream): unknown {
    const settings = stream.getVideoTracks()[0]?.getSettings();
    const w = settings?.width ?? 1280;
    const h = settings?.height ?? 720;
    const fov = (60 * Math.PI) / 180;
    const fx = w / (2 * Math.tan(fov / 2));
    return { fx, fy: fx, cx: w / 2, cy: h / 2, w, h };
  }

  private send(payload: unknown): void {
    this.ws?.send(JSON.stringify(payload));
  }
}
