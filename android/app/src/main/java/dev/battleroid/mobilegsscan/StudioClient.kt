package dev.battleroid.mobilegsscan

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
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
 *
 * The WebSocket frame ingest is handled by StreamingClient. This
 * class is HTTP-only.
 */
class StudioClient(private val baseUrl: String) {
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
    private data class CaptureCreate(
        val name: String,
        val source: String = "mobile_native",
        val has_pose: Boolean = true,
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

    suspend fun createCapture(name: String, hasPose: Boolean = true): Capture =
        withContext(Dispatchers.IO) {
            val payload = json.encodeToString(
                CaptureCreate.serializer(),
                CaptureCreate(name = name, has_pose = hasPose),
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
