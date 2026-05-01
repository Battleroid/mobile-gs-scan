package dev.battleroid.mobilegsscan

import android.os.Bundle
import android.text.InputType
import android.view.Gravity
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.updatePadding

class ServerConfigActivity : AppCompatActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val padding = (24 * resources.displayMetrics.density).toInt()
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(padding, padding, padding, padding)
        }

        val label = TextView(this).apply {
            text = getString(R.string.label_studio_url)
        }
        val input = EditText(this).apply {
            hint = getString(R.string.hint_studio_url)
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_URI
            setText(ServerConfig.studioUrl(this@ServerConfigActivity).orEmpty())
        }
        // Helper text under the input — clarifies that the scheme is
        // optional (we auto-prefix `https://` in ServerConfig.normalize)
        // and reminds the user `http://` is supported if they need it.
        val helper = TextView(this).apply {
            text = getString(R.string.hint_studio_url_help)
            textSize = 12f
            setTextColor(getColor(R.color.muted))
            gravity = Gravity.START
            val topMargin = (4 * resources.displayMetrics.density).toInt()
            val bottomMargin = (16 * resources.displayMetrics.density).toInt()
            (layoutParams as? LinearLayout.LayoutParams)?.apply {
                this.topMargin = topMargin
                this.bottomMargin = bottomMargin
            }
        }
        val save = Button(this).apply {
            text = getString(R.string.action_save)
            setOnClickListener {
                val raw = input.text.toString().trim()
                if (raw.isEmpty()) {
                    Toast.makeText(
                        this@ServerConfigActivity,
                        "studio URL required",
                        Toast.LENGTH_SHORT,
                    ).show()
                    return@setOnClickListener
                }
                // ServerConfig.setStudioUrl runs the same normalize()
                // (trim, drop trailing slash, prepend https:// if no
                // scheme), so a user typing bare `192.168.1.42` saves
                // as `https://192.168.1.42`. Echo what we actually
                // saved so the user sees the auto-prefix happen
                // instead of wondering why their bare IP "didn't
                // work" — Android's habit of auto-correcting a typed
                // URL to a search query has trained everyone to
                // distrust this kind of input.
                ServerConfig.setStudioUrl(this@ServerConfigActivity, raw)
                val saved = ServerConfig.studioUrl(this@ServerConfigActivity).orEmpty()
                if (saved != raw) {
                    Toast.makeText(
                        this@ServerConfigActivity,
                        "saved as $saved",
                        Toast.LENGTH_SHORT,
                    ).show()
                }
                finish()
            }
        }

        root.addView(label)
        root.addView(input)
        root.addView(helper)
        root.addView(save)
        setContentView(root)

        // Apply the helper-text margins after addView so the layout
        // params are the LinearLayout-owned ones (LinearLayout.LayoutParams).
        // Setting layoutParams during the apply{} block on TextView would
        // be ViewGroup.LayoutParams, which doesn't have topMargin /
        // bottomMargin fields — and Kotlin's safe-cast above would
        // silently no-op. Re-apply now that the parent is in place.
        (helper.layoutParams as LinearLayout.LayoutParams).apply {
            topMargin = (4 * resources.displayMetrics.density).toInt()
            bottomMargin = (16 * resources.displayMetrics.density).toInt()
            helper.layoutParams = this
        }

        // Same edge-to-edge handling MainActivity gets: pad the root
        // with the systemBars insets ON TOP of the existing 24dp
        // baseline padding so the form fields don't slide under the
        // status bar / gesture nav on Android 15+.
        val basePad = padding
        ViewCompat.setOnApplyWindowInsetsListener(root) { v, insets ->
            val sys = insets.getInsets(WindowInsetsCompat.Type.systemBars())
            v.updatePadding(
                left = basePad + sys.left,
                top = basePad + sys.top,
                right = basePad + sys.right,
                bottom = basePad + sys.bottom,
            )
            insets
        }
    }
}
