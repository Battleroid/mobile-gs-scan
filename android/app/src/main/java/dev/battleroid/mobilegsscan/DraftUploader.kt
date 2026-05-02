package dev.battleroid.mobilegsscan

import android.content.Context
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeoutOrNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put

/**
 * Replays a [Draft] directory through the existing
 * [StreamingClient] WebSocket pipeline:
 *
 *   1. POST /api/captures with the draft's name + train_iters meta.
 *      Server returns capture id + pair_token.
 *   2. Open the WS, send the session header (using the intrinsics
 *      stamped onto the draft when its first frame was recorded).
 *   3. Walk poses.jsonl in order. For each line, send the frame
 *      JPEG + header to the server. Yield briefly between sends so
 *      the WS write loop doesn't block the activity's main thread.
 *   4. Send finalize.
 *   5. **Wait for the server's "queued" event before deleting the
 *      draft directory.** This is the durability guarantee the user
 *      asked for: if the upload fails partway, the local data
 *      stays put and the user can retry.
 *
 * The whole thing is one suspendable function with a single
 * [Result] return value so callers get an "ok or here's why not"
 * shape they can map straight to UI state. Progress is reported
 * through [onProgress] callbacks (frames sent / total).
 */
class DraftUploader(
    private val ctx: Context,
    private val baseUrl: String,
    private val client: StudioClient,
) {

    sealed class Result {
        data class Ok(val captureId: String, val sceneId: String?) : Result()
        data class Failed(val reason: String) : Result()
    }

    suspend fun upload(
        scope: CoroutineScope,
        draft: Draft,
        onProgress: (sent: Int, total: Int) -> Unit = { _, _ -> },
    ): Result = withContext(Dispatchers.IO) {
        val meta = draft.meta
        val total = meta.frame_count
        if (total == 0) {
            return@withContext Result.Failed("draft has no frames")
        }

        // Build the per-capture meta blob exactly the way the
        // home-screen createNewCapture flow used to. The worker's
        // dispatch step reads ``train_iters`` from this to specialize
        // the splatfacto job for this capture; everything else is
        // ignored by the server.
        val createMeta: JsonObject = buildJsonObject {
            meta.train_iters?.let { put("train_iters", JsonPrimitive(it)) }
        }

        val capture = try {
            client.createCapture(
                name = meta.name,
                hasPose = true,
                source = "mobile_native",
                meta = createMeta,
            )
        } catch (e: Exception) {
            return@withContext Result.Failed("createCapture failed: ${e.message ?: "unknown"}")
        }

        val pairToken = capture.pair_token
            ?: return@withContext Result.Failed("server returned no pair_token")

        // Use a child scope so we can hard-cancel the StreamingClient
        // listener if anything goes wrong mid-upload without
        // bleeding into the caller's scope.
        val streamerScope = scope
        val streamer = StreamingClient(
            scope = streamerScope,
            baseUrl = baseUrl,
            captureId = capture.id,
            pairToken = pairToken,
        )
        streamer.connect()

        try {
            val intr = meta.intrinsics?.toIntrinsics()
                ?: Intrinsics(0f, 0f, 0f, 0f, 0, 0)
            streamer.sendSession(
                deviceLabel = android.os.Build.MODEL,
                intrinsics = intr,
                hasPose = true,
            )

            // Walk the draft and replay every frame. Sequential is
            // fine: even at 600 frames × ~150 KB ≈ 90 MB total, a
            // local-network WS easily saturates a couple thousand
            // frames per minute. We yield between frames so the
            // streamer's listener coroutine + UI updates aren't
            // starved.
            var sent = 0
            for (line in draft.readPoseLines()) {
                val jpeg = draft.readFrameJpeg(line.idx) ?: continue
                streamer.sendFrame(
                    idx = line.idx,
                    jpeg = jpeg,
                    pose = line.pose,
                    intrinsics = intr,
                )
                sent += 1
                onProgress(sent, total)
                // 5 ms between frames is plenty to keep the UI
                // responsive without choking throughput. We're
                // bounded by the WS send queue, not by this delay.
                delay(5)
            }

            streamer.finalize("user")

            // Wait up to 30s for the server's "queued" event. That
            // event is what tells us the capture is durable on the
            // server side — only after it lands do we delete the
            // local draft.
            val queued = withTimeoutOrNull(30_000L) {
                streamer.events.first { it is StreamingClient.Event.Queued }
                    as StreamingClient.Event.Queued
            } ?: return@withContext Result.Failed(
                "timed out waiting for server to acknowledge upload",
            )

            // Server has the data. Safe to drop the local copy.
            draft.delete()
            return@withContext Result.Ok(
                captureId = capture.id,
                sceneId = queued.sceneId.takeIf { it.isNotBlank() },
            )
        } catch (e: Exception) {
            return@withContext Result.Failed("upload failed: ${e.message ?: "unknown"}")
        } finally {
            streamer.close()
        }
    }
}

private fun SerializableIntrinsics.toIntrinsics(): Intrinsics =
    Intrinsics(fx = fx, fy = fy, cx = cx, cy = cy, w = w, h = h)
