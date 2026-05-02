package dev.battleroid.mobilegsscan

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import java.util.concurrent.TimeUnit

/**
 * Coroutine-friendly HTTP client for the studio API.
 *
 * Two responsibilities:
 *   1. Health check (`GET /api/health`) so the home screen can show
 *      an online/offline indicator.
 *   2. Capture-session lifecycle:
 *        GET  /api/captures            — list for the home screen
 *        POST /api/captures            — start a phone-driven session
 *        GET  /api/captures/{id}       — detail for the native
 *                                       session screen
 *        GET  /api/scenes/{id}         — scene + embedded jobs list
 *        GET  /api/jobs/{id}           — single-job detail
 *        GET  /api/jobs/{id}/log       — tail of the subprocess log
 *                                       (sfm: glomap.log; train:
 *                                       train.log; export: export.log)
 *
 * The WebSocket frame ingest is handled by StreamingClient. This
 * class is HTTP-only.
 */
class StudioClient(private val baseUrl: String) {
    // encodeDefaults = false here is *intentional* for the response-
    // parsing direction (lets us add fields server-side without
    // forcing them into every request DTO). On the request side it
    // means request DTOs must NOT carry defaults on fields that the
    // server interprets differently than kotlin's intent — see
    // CaptureCreate below for the ugly version of that lesson.
    private val json = Json { ignoreUnknownKeys = true; encodeDefaults = false }

    private val http: OkHttpClient = OkHttpClient.Builder()
        .connectTimeout(5, TimeUnit.SECONDS)
        .readTimeout(15, TimeUnit.SECONDS)
        .build()

    @Serializable
    data class Capture(
        val id: String,
        val name: String,
        val status: String,
        val source: String,
        val pair_token: String? = null,
        val pair_url: String? = null,
        val frame_count: Int = 0,
        val dropped_count: Int = 0,
        val has_pose: Boolean = false,
        val scene_id: String? = null,
        val error: String? = null,
        val created_at: String,
        val updated_at: String,
    )

    @Serializable
    data class JobView(
        val id: String,
        val kind: String,
        val status: String,
        val progress: Float = 0f,
        val progress_msg: String? = null,
        val error: String? = null,
    )

    @Serializable
    data class Scene(
        val id: String,
        val capture_id: String,
        val status: String,
        val error: String? = null,
        val ply_url: String? = null,
        val spz_url: String? = null,
        val jobs: List<JobView> = emptyList(),
        val created_at: String,
        val completed_at: String? = null,
    )

    @Serializable
    data class JobDetail(
        val id: String,
        val scene_id: String,
        val kind: String,
        val status: String,
        val progress: Float = 0f,
        val progress_msg: String? = null,
        val error: String? = null,
        val claimed_by: String? = null,
        val started_at: String? = null,
        val completed_at: String? = null,
        val result: JsonElement? = null,
    )

    @Serializable
    data class JobLog(
        val log: String = "",
        val size: Long = 0L,
        val path: String? = null,
        val available: Boolean = false,
    )

    // Request DTO: no defaults on any field (see comment on the
    // ``json`` instance above; encodeDefaults=false would silently
    // drop them otherwise). ``meta`` is a JsonObject so callers can
    // pass arbitrary key/value pairs (currently just
    // ``train_iters``) without us having to invent a typed shape
    // every time we add an optional knob.
    @Serializable
    private data class CaptureCreate(
        val name: String,
        val source: String,
        val has_pose: Boolean,
        val meta: JsonObject,
    )

    suspend fun health(): Boolean = withContext(Dispatchers.IO) {
        runCatching {
            http.newCall(Request.Builder().url("$baseUrl/api/health").build())
                .execute()
                .use { it.isSuccessful }
        }.getOrDefault(false)
    }

    suspend fun listCaptures(): List<Capture> = withContext(Dispatchers.IO) {
        http.newCall(Request.Builder().url("$baseUrl/api/captures").build())
            .execute()
            .use { res ->
                if (!res.isSuccessful) error("HTTP ${res.code}")
                val body = res.body?.string().orEmpty()
                json.decodeFromString<List<Capture>>(body)
            }
    }

    suspend fun getCapture(id: String): Capture = withContext(Dispatchers.IO) {
        http.newCall(Request.Builder().url("$baseUrl/api/captures/$id").build())
            .execute()
            .use { res ->
                if (!res.isSuccessful) error("HTTP ${res.code}")
                json.decodeFromString<Capture>(res.body?.string().orEmpty())
            }
    }

    suspend fun getScene(id: String): Scene = withContext(Dispatchers.IO) {
        http.newCall(Request.Builder().url("$baseUrl/api/scenes/$id").build())
            .execute()
            .use { res ->
                if (!res.isSuccessful) error("HTTP ${res.code}")
                json.decodeFromString<Scene>(res.body?.string().orEmpty())
            }
    }

    suspend fun getJob(id: String): JobDetail = withContext(Dispatchers.IO) {
        http.newCall(Request.Builder().url("$baseUrl/api/jobs/$id").build())
            .execute()
            .use { res ->
                if (!res.isSuccessful) error("HTTP ${res.code}")
                json.decodeFromString<JobDetail>(res.body?.string().orEmpty())
            }
    }

    /**
     * Fetch the tail of the subprocess log file for [id]. Server
     * caps tailBytes at 1 MB; default 16 KB here gives the user a
     * reasonable on-screen tail without making the response huge.
     * Mesh job kind responds with available=false (no subprocess in
     * PR #1 stub).
     */
    suspend fun getJobLog(id: String, tailBytes: Int = 16384): JobLog =
        withContext(Dispatchers.IO) {
            val req = Request.Builder()
                .url("$baseUrl/api/jobs/$id/log?tail_bytes=$tailBytes")
                .build()
            http.newCall(req).execute().use { res ->
                if (!res.isSuccessful) error("HTTP ${res.code}")
                json.decodeFromString<JobLog>(res.body?.string().orEmpty())
            }
        }

    /**
     * Create a new capture session. ``meta`` is a free-form bag of
     * per-capture overrides the worker's dispatch step reads to
     * specialize the pipeline — currently the only key is
     * ``train_iters`` (per-capture splatfacto iter count override).
     */
    suspend fun createCapture(
        name: String,
        hasPose: Boolean = true,
        source: String = "mobile_native",
        meta: JsonObject = JsonObject(emptyMap()),
    ): Capture = withContext(Dispatchers.IO) {
        val payload = json.encodeToString(
            CaptureCreate.serializer(),
            CaptureCreate(
                name = name,
                source = source,
                has_pose = hasPose,
                meta = meta,
            ),
        )
        val req = Request.Builder()
            .url("$baseUrl/api/captures")
            .post(payload.toRequestBody("application/json".toMediaType()))
            .build()
        http.newCall(req).execute().use { res ->
            if (!res.isSuccessful) {
                error("HTTP ${res.code}: ${res.body?.string().orEmpty()}")
            }
            val body = res.body?.string().orEmpty()
            json.decodeFromString<Capture>(body)
        }
    }
}
