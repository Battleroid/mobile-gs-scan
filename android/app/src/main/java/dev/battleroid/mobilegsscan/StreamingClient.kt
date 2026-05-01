package dev.battleroid.mobilegsscan

import android.util.Log
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.launch
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import kotlinx.serialization.json.putJsonArray
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString.Companion.toByteString
import java.util.concurrent.TimeUnit

/**
 * Phone → server WebSocket frame streamer.
 *
 * Protocol (alternating per frame):
 *   1) JSON header  {"type":"frame","idx":N,"ts":<ms>,"pose":...,"intrinsics":...}
 *   2) raw JPEG bytes for that idx
 * Plus the lifecycle messages (session/heartbeat/finalize).
 *
 * Owns its OkHttp client + lifetime — call [close] when the activity
 * tears down. Emits [Event] values through [events].
 */
class StreamingClient(
    private val scope: CoroutineScope,
    baseUrl: String,
    captureId: String,
    pairToken: String,
) {

    sealed class Event {
        data class Ack(val received: Int, val dropped: Int) : Event()
        data class Limit(val reason: String, val cap: Int) : Event()
        data class Queued(val sceneId: String) : Event()
        data class Closed(val reason: String?) : Event()
        data class Failed(val error: String) : Event()
    }

    private val json = Json { ignoreUnknownKeys = true }
    private val httpClient = OkHttpClient.Builder()
        .pingInterval(20, TimeUnit.SECONDS)
        .readTimeout(0, TimeUnit.SECONDS)
        .build()

    private val wsUrl = run {
        val ws = baseUrl.replaceFirst("http", "ws")
        "$ws/api/captures/$captureId/stream?token=$pairToken"
    }

    private val _events = MutableSharedFlow<Event>(extraBufferCapacity = 32)
    val events: SharedFlow<Event> = _events.asSharedFlow()

    private var ws: WebSocket? = null

    fun connect() {
        val req = Request.Builder().url(wsUrl).build()
        ws = httpClient.newWebSocket(req, object : WebSocketListener() {
            override fun onMessage(webSocket: WebSocket, text: String) {
                handleControl(text)
            }
            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                scope.launch { _events.emit(Event.Closed(reason)) }
            }
            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                Log.w("Streamer", "ws failure", t)
                scope.launch { _events.emit(Event.Failed(t.message ?: "ws failure")) }
            }
        })
    }

    fun sendSession(deviceLabel: String, intrinsics: Intrinsics, hasPose: Boolean) {
        val payload = buildJsonObject {
            put("type", "session")
            put("device", buildJsonObject { put("label", deviceLabel) })
            put("intrinsics", intrinsics.toJson())
            put("has_pose", hasPose)
        }
        ws?.send(json.encodeToString(JsonElement.serializer(), payload))
    }

    /**
     * Send one frame as a header text message immediately followed by
     * the binary JPEG bytes. The server pairs them in order.
     */
    fun sendFrame(idx: Int, jpeg: ByteArray, pose: FloatArray?, intrinsics: Intrinsics?) {
        val header = buildJsonObject {
            put("type", "frame")
            put("idx", idx)
            put("ts", System.currentTimeMillis())
            if (pose != null) {
                putJsonArray("pose") {
                    for (v in pose) add(kotlinx.serialization.json.JsonPrimitive(v))
                }
            }
            if (intrinsics != null) {
                put("intrinsics", intrinsics.toJson())
            }
        }
        val sock = ws ?: return
        sock.send(json.encodeToString(JsonElement.serializer(), header))
        sock.send(jpeg.toByteString())
    }

    fun heartbeat() {
        ws?.send("""{"type":"heartbeat","ts":${System.currentTimeMillis()}}""")
    }

    fun finalize(reason: String = "user") {
        ws?.send("""{"type":"finalize","reason":"$reason"}""")
    }

    fun close() {
        ws?.close(1000, "client closing")
        ws = null
    }

    private fun handleControl(text: String) {
        val obj = runCatching { json.parseToJsonElement(text).jsonObject }.getOrNull()
            ?: return
        val type = obj["type"]?.jsonPrimitive?.content ?: return
        scope.launch(Dispatchers.Default) {
            when (type) {
                "ack" -> _events.emit(
                    Event.Ack(
                        obj["frames_received"]?.intOrZero() ?: 0,
                        obj["frames_dropped"]?.intOrZero() ?: 0,
                    ),
                )
                "limit" -> _events.emit(
                    Event.Limit(
                        obj["reason"]?.jsonPrimitive?.content ?: "unknown",
                        obj["cap"]?.intOrZero() ?: 0,
                    ),
                )
                "queued" -> _events.emit(
                    Event.Queued(obj["scene_id"]?.jsonPrimitive?.content.orEmpty()),
                )
            }
        }
    }
}

/** ARCore-supplied pinhole intrinsics (px units). */
@Serializable
data class Intrinsics(
    val fx: Float,
    val fy: Float,
    val cx: Float,
    val cy: Float,
    val w: Int,
    val h: Int,
) {
    fun toJson(): JsonObject = buildJsonObject {
        put("fx", fx); put("fy", fy); put("cx", cx); put("cy", cy)
        put("w", w); put("h", h)
    }
}

private fun JsonElement.intOrZero(): Int =
    runCatching { jsonPrimitive.content.toInt() }.getOrDefault(0)
