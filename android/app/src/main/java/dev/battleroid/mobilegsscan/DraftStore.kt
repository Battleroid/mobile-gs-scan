package dev.battleroid.mobilegsscan

import android.content.Context
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import java.io.File
import java.io.FileOutputStream
import java.io.OutputStreamWriter
import java.time.Instant
import java.util.UUID

/**
 * On-device capture-draft store.
 *
 * The app records every capture session to local storage first
 * (frames + poses on disk), then asks the user whether to upload
 * the draft to the studio now or save it for later. This decouples
 * the capture step from the network: the user can record a draft
 * while away from the studio's network and upload when reconnected.
 *
 * On-disk layout, rooted at ``filesDir/captures/<draft-id>/``:
 *
 *   ├─ meta.json       DraftMeta — id, name (nullable, server fills
 *   │                  in a random one if blank at upload), created/
 *   │                  updated timestamps, frame_count, finalized,
 *   │                  intrinsics (set on first frame), train_iters
 *   │                  (per-capture splatfacto override).
 *   ├─ frames/
 *   │  └─ NNNNNN.jpg   one JPEG per ARCore frame, idx zero-padded
 *   └─ poses.jsonl     append-only line file; one JSON object per
 *                      ARCore frame: {idx, pose: [16 floats],
 *                      intrinsics, ts}. Indices match the JPEGs in
 *                      frames/. Currently retained for future
 *                      ARCore-poses upload paths; the HTTP upload
 *                      flow uses only the JPEG frames.
 *
 * Lifecycle:
 *   - [DraftStore.newDraft] creates a fresh dir + meta, returns a
 *     [Draft] handle.
 *   - The capture activity calls [Draft.appendFrame] per ARCore
 *     frame; [DraftStore] flushes meta after every append so an
 *     app crash mid-capture leaves a usable partial draft.
 *   - On Finish, the user picks Upload Now / Save for Later /
 *     Discard. Upload Now hands off to [DraftUploader] which
 *     replays the directory through the existing WebSocket
 *     pipeline; Save for Later flips ``finalized = true`` and
 *     leaves the dir alone; Discard calls [Draft.delete].
 *   - [DraftUploader] deletes the directory only after the server
 *     acknowledges the capture has been queued (i.e. the data is
 *     safely on the studio's disk).
 *
 * Drafts list ordering: most-recently-modified first, scanned
 * directly from the filesystem on each call (no separate index —
 * the directory IS the index).
 */
class Draft internal constructor(
    val id: String,
    val dir: File,
) {
    private val metaFile = File(dir, "meta.json")
    private val framesDir = File(dir, "frames").apply { mkdirs() }
    private val posesFile = File(dir, "poses.jsonl")

    private val json = Json { prettyPrint = false; encodeDefaults = false }
    private val prettyJson = Json { prettyPrint = true; encodeDefaults = false }

    @Volatile private var _meta: DraftMeta = readOrInitMeta()
    val meta: DraftMeta get() = _meta

    /**
     * Append [jpeg] + [pose] + [intrinsics] for frame [idx]. Bumps
     * frame_count and updates updated_at. The intrinsics are
     * stamped onto the meta on the first frame so the upload path
     * has a single source of truth (every ARCore frame in a
     * session reports the same intrinsics anyway).
     */
    @Synchronized
    fun appendFrame(idx: Int, jpeg: ByteArray, pose: FloatArray, intrinsics: Intrinsics) {
        val frameFile = File(framesDir, String.format("%06d.jpg", idx))
        frameFile.writeBytes(jpeg)

        val line = buildJsonObject {
            put("idx", JsonPrimitive(idx))
            put("pose", buildJsonArray {
                for (v in pose) add(JsonPrimitive(v))
            })
            put("intrinsics", intrinsics.toJson())
            put("ts", JsonPrimitive(System.currentTimeMillis()))
        }
        // Append-only writer; manually flushed so a crash mid-line
        // doesn't truncate previously-written lines.
        FileOutputStream(posesFile, true).use { fos ->
            OutputStreamWriter(fos, Charsets.UTF_8).use { osw ->
                osw.write(json.encodeToString(JsonObject.serializer(), line))
                osw.write("\n")
                osw.flush()
                fos.fd.sync()
            }
        }

        _meta = _meta.copy(
            frame_count = _meta.frame_count + 1,
            updated_at = nowIso(),
            intrinsics = _meta.intrinsics ?: SerializableIntrinsics(
                fx = intrinsics.fx, fy = intrinsics.fy,
                cx = intrinsics.cx, cy = intrinsics.cy,
                w = intrinsics.w, h = intrinsics.h,
            ),
        )
        writeMeta()
    }

    /** Mark the draft as user-finalized (Finish or Save-for-later). */
    @Synchronized
    fun finalize() {
        _meta = _meta.copy(finalized = true, updated_at = nowIso())
        writeMeta()
    }

    /** Set the user-facing name. Trimmed by caller. Pass null to clear. */
    @Synchronized
    fun setName(name: String?) {
        _meta = _meta.copy(
            name = name?.takeIf { it.isNotBlank() },
            updated_at = nowIso(),
        )
        writeMeta()
    }

    /** Set the per-capture train_iters override. */
    @Synchronized
    fun setTrainIters(iters: Int?) {
        _meta = _meta.copy(train_iters = iters, updated_at = nowIso())
        writeMeta()
    }

    fun delete() {
        dir.deleteRecursively()
    }

    /** Walk poses.jsonl yielding parsed entries in idx order. */
    fun readPoseLines(): Sequence<PoseLine> = sequence {
        if (!posesFile.exists()) return@sequence
        posesFile.bufferedReader(Charsets.UTF_8).useLines { lines ->
            for (raw in lines) {
                if (raw.isBlank()) continue
                val obj = runCatching {
                    json.parseToJsonElement(raw).jsonObject
                }.getOrNull() ?: continue
                val idx = obj["idx"]?.jsonPrimitive?.content?.toIntOrNull() ?: continue
                val poseArr = obj["pose"]?.jsonArray ?: continue
                val pose = FloatArray(poseArr.size) { i ->
                    poseArr[i].jsonPrimitive.content.toFloatOrNull() ?: 0f
                }
                yield(PoseLine(idx = idx, pose = pose))
            }
        }
    }

    fun readFrameJpeg(idx: Int): ByteArray? {
        val f = File(framesDir, String.format("%06d.jpg", idx))
        return if (f.exists()) f.readBytes() else null
    }

    /**
     * Sorted list of JPEG frame Files in the draft. Used by the
     * HTTP-upload path so the uploader can stream the bytes
     * directly off disk rather than loading them into RAM.
     */
    fun frameFiles(): List<File> {
        val files = framesDir.listFiles { f -> f.isFile && f.name.endsWith(".jpg") }
        return files?.sortedBy { it.name }.orEmpty()
    }

    /**
     * Path to the draft's poses.jsonl, or null if no frames have
     * been recorded yet (and so no poses written). The HTTP-upload
     * path ships this file alongside the JPEG frames so the worker's
     * SfM step can route through the cheap arcore_native backend
     * instead of re-running glomap from scratch.
     */
    fun posesFileOrNull(): File? = posesFile.takeIf { it.exists() && it.length() > 0 }

    /** Path to a representative thumbnail (first frame). May not exist. */
    fun thumbnailFile(): File? {
        val first = framesDir.listFiles()?.minByOrNull { it.name }
        return first
    }

    private fun readOrInitMeta(): DraftMeta {
        return if (metaFile.exists()) {
            runCatching {
                prettyJson.decodeFromString(DraftMeta.serializer(), metaFile.readText())
            }.getOrElse {
                // Corrupt meta file — re-initialize but keep any
                // already-recorded frames discoverable on disk.
                DraftMeta(
                    id = id,
                    created_at = nowIso(),
                    updated_at = nowIso(),
                ).also { writeMetaFromDefault(it) }
            }
        } else {
            DraftMeta(
                id = id,
                created_at = nowIso(),
                updated_at = nowIso(),
            ).also { writeMetaFromDefault(it) }
        }
    }

    private fun writeMetaFromDefault(m: DraftMeta) {
        metaFile.writeText(prettyJson.encodeToString(DraftMeta.serializer(), m))
    }

    private fun writeMeta() {
        // Atomic-ish: write to temp file then rename. SQLite-style.
        val tmp = File(dir, "meta.json.tmp")
        tmp.writeText(prettyJson.encodeToString(DraftMeta.serializer(), _meta))
        if (!tmp.renameTo(metaFile)) {
            // Fall back to direct overwrite if rename fails (e.g.
            // some filesystems on certain emulators); still better
            // than losing the meta entirely.
            metaFile.writeText(prettyJson.encodeToString(DraftMeta.serializer(), _meta))
            tmp.delete()
        }
    }

    data class PoseLine(val idx: Int, val pose: FloatArray)
}

@Serializable
data class DraftMeta(
    val id: String,
    val name: String? = null,
    val created_at: String,
    val updated_at: String,
    val frame_count: Int = 0,
    val finalized: Boolean = false,
    val intrinsics: SerializableIntrinsics? = null,
    val train_iters: Int? = null,
)

@Serializable
data class SerializableIntrinsics(
    val fx: Float,
    val fy: Float,
    val cx: Float,
    val cy: Float,
    val w: Int,
    val h: Int,
)

object DraftStore {
    private fun root(ctx: Context): File =
        File(ctx.filesDir, "captures").apply { mkdirs() }

    /**
     * Allocate a new draft directory with a fresh id + initial
     * meta. The caller can pass [trainIters] (the user's current
     * training-fidelity preset) so we replay the same value at
     * upload time even if the user changes the preset between
     * recording and uploading.
     */
    fun newDraft(ctx: Context, trainIters: Int? = null): Draft {
        val id = UUID.randomUUID().toString().replace("-", "").take(16)
        val dir = File(root(ctx), id).apply { mkdirs() }
        val draft = Draft(id, dir)
        if (trainIters != null) draft.setTrainIters(trainIters)
        return draft
    }

    /**
     * Open an existing draft by id. Returns null if the directory
     * doesn't exist; that's the caller's signal to drop a stale
     * intent extra and route home.
     */
    fun openDraft(ctx: Context, id: String): Draft? {
        val dir = File(root(ctx), id)
        if (!dir.isDirectory) return null
        return Draft(id, dir)
    }

    /**
     * List all drafts on disk, most-recently-modified first.
     * Drafts are scanned directly from the filesystem; meta.json is
     * the source of truth.
     */
    fun listDrafts(ctx: Context): List<Draft> {
        val rootDir = root(ctx)
        val children = rootDir.listFiles { f -> f.isDirectory } ?: return emptyList()
        return children
            .sortedByDescending { it.lastModified() }
            .map { Draft(it.name, it) }
    }

    fun deleteAllDrafts(ctx: Context) {
        root(ctx).deleteRecursively()
    }
}

private fun nowIso(): String = Instant.now().toString()
