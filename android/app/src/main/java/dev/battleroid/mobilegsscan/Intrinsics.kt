package dev.battleroid.mobilegsscan

import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject

/**
 * Pinhole camera intrinsics for one frame. ARCore exposes these
 * via `Frame.camera.imageIntrinsics`; the worker's SfM step expects
 * them on each line of `poses.jsonl` (and on the per-capture
 * `meta.json`'s ``intrinsics`` field).
 *
 * Lived inside the old `StreamingClient.kt` together with the
 * WebSocket frame protocol. Lifted out into its own file when the
 * WS path was retired so `ARCaptureSession` and `DraftStore` keep
 * compiling — they still need a structured shape for per-frame
 * intrinsics, even though uploads now go over HTTP.
 *
 * Floats for `fx` / `fy` / `cx` / `cy` (pixels), Ints for the
 * image dimensions.
 */
data class Intrinsics(
    val fx: Float,
    val fy: Float,
    val cx: Float,
    val cy: Float,
    val w: Int,
    val h: Int,
) {
    fun toJson(): JsonObject = buildJsonObject {
        put("fx", JsonPrimitive(fx))
        put("fy", JsonPrimitive(fy))
        put("cx", JsonPrimitive(cx))
        put("cy", JsonPrimitive(cy))
        put("w", JsonPrimitive(w))
        put("h", JsonPrimitive(h))
    }
}
