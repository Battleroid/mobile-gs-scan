package dev.battleroid.mobilegsscan

import android.content.Context
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject

/**
 * Replays a [Draft] directory through the studio's HTTP upload API:
 *
 *   1. POST /api/captures with the draft's name + train_iters meta.
 *      ``has_pose=true`` when the draft has a poses.jsonl on disk
 *      (typical case — every ARCore frame writes one); the
 *      dispatcher uses that to pick the cheap ``arcore_native`` SfM
 *      backend (transforms.json built from the phone's poses, no
 *      glomap run). Falls back to ``has_pose=false`` for
 *      poseless drafts so the server runs real SfM via glomap.
 *   2. POST /api/captures/{id}/upload (multipart) for each batch of
 *      JPEG frames, walked sorted off disk.
 *   3. POST /api/captures/{id}/poses with the draft's poses.jsonl
 *      (skipped on poseless drafts). Sent as a single .jsonl body.
 *   4. POST /api/captures/{id}/finalize to flip the capture into
 *      ``queued`` and trigger the worker pipeline.
 *   5. **Wait for the server's finalize response** before deleting
 *      the draft directory. If anything fails partway, the local
 *      data stays put and the user can retry.
 *
 * The whole thing is one suspendable function with a single
 * [Result] return value so callers get an "ok or here's why not"
 * shape they can map straight to UI state. Progress is reported
 * through [onProgress] callbacks (frames sent / total).
 *
 * History note: the previous implementation used a WebSocket
 * frame-stream + pair-token endpoint that's been removed.
 */
class DraftUploader(
    @Suppress("unused") private val ctx: Context,
    @Suppress("unused") private val baseUrl: String,
    private val client: StudioClient,
) {

    sealed class Result {
        data class Ok(val captureId: String, val sceneId: String?) : Result()
        data class Failed(val reason: String) : Result()
    }

    /**
     * Upload [draft] to the studio. Reports per-frame progress via
     * [onProgress] (sent / total). Deletes the draft on success.
     */
    suspend fun upload(
        @Suppress("UNUSED_PARAMETER") scope: CoroutineScope,
        draft: Draft,
        onProgress: (sent: Int, total: Int) -> Unit = { _, _ -> },
    ): Result = withContext(Dispatchers.IO) {
        val meta = draft.meta
        val frames = draft.frameFiles()
        val total = frames.size
        if (total == 0) {
            return@withContext Result.Failed("draft has no frames")
        }
        val poses = draft.posesFileOrNull()
        val hasPose = poses != null

        val createMeta: JsonObject = buildJsonObject {
            put("source_kind", JsonPrimitive("images"))
            put("count", JsonPrimitive(total))
            meta.train_iters?.let { put("train_iters", JsonPrimitive(it)) }
        }

        val capture = try {
            client.createCapture(
                name = meta.name,
                hasPose = hasPose,
                source = "upload",
                meta = createMeta,
            )
        } catch (e: Exception) {
            return@withContext Result.Failed(
                "createCapture failed: ${e.message ?: "unknown"}",
            )
        }

        // Batch frames so a single multipart body doesn't grow
        // unbounded. Server accepts up to a few hundred per request
        // comfortably; 50 keeps each batch around ~5–10 MB at typical
        // ARCore JPEG sizes.
        val batchSize = 50
        var sent = 0
        try {
            for (start in frames.indices step batchSize) {
                val batch = frames.subList(start, minOf(start + batchSize, total))
                client.uploadFrames(capture.id, batch)
                sent += batch.size
                onProgress(sent, total)
            }
            if (poses != null) {
                client.uploadPoses(capture.id, poses)
            }
            val sceneId = client.finalizeCapture(capture.id)
            // Server has the data + the scene row. Safe to drop the
            // local copy.
            draft.delete()
            return@withContext Result.Ok(
                captureId = capture.id,
                sceneId = sceneId.ifBlank { null },
            )
        } catch (e: Exception) {
            return@withContext Result.Failed(
                "upload failed: ${e.message ?: "unknown"}",
            )
        }
    }
}
