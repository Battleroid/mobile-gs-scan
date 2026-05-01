package dev.battleroid.mobilegsscan

import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.widget.Button
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity

/**
 * Entry / settings activity. Three responsibilities:
 *   1. Show the current connection status + a Settings button.
 *   2. Handle deep-link intents (https://<studio>/m/<token>) by
 *      routing into [CaptureActivity] without a browser round-trip.
 *   3. Persist the studio URL inferred from the first deep link —
 *      so scanning the QR also configures the app.
 */
class MainActivity : AppCompatActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        findViewById<Button>(R.id.btnSettings).setOnClickListener {
            startActivity(Intent(this, ServerConfigActivity::class.java))
        }

        handleIntent(intent)
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        handleIntent(intent)
    }

    override fun onResume() {
        super.onResume()
        val status = findViewById<TextView>(R.id.status)
        status.text = when {
            ServerConfig.studioUrl(this) == null -> getString(R.string.status_no_url)
            else -> getString(R.string.status_idle)
        }
    }

    private fun handleIntent(intent: Intent?) {
        val data: Uri = intent?.data ?: return
        val token = parsePairToken(data) ?: return
        val baseUrl = "${data.scheme}://${data.host}${if (data.port > 0) ":${data.port}" else ""}"

        // Persist the studio URL we just learned, in case the user
        // never visited Settings.
        ServerConfig.setStudioUrl(this, baseUrl)

        val launch = Intent(this, CaptureActivity::class.java).apply {
            putExtra(CaptureActivity.EXTRA_BASE_URL, baseUrl)
            putExtra(CaptureActivity.EXTRA_PAIR_TOKEN, token)
        }
        startActivity(launch)
    }

    /** Extract `<token>` from `/m/<token>` paths. */
    private fun parsePairToken(data: Uri): String? {
        val segments = data.pathSegments
        if (segments.size < 2) return null
        if (segments[0] != "m") return null
        return segments[1].takeIf { it.isNotBlank() }
    }
}
