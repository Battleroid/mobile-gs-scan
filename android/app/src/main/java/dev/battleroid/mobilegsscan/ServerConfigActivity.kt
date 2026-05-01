package dev.battleroid.mobilegsscan

import android.os.Bundle
import android.text.InputType
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
        val save = Button(this).apply {
            text = getString(R.string.action_save)
            setOnClickListener {
                val url = input.text.toString().trim()
                if (url.isEmpty()) {
                    Toast.makeText(
                        this@ServerConfigActivity,
                        "studio URL required",
                        Toast.LENGTH_SHORT,
                    ).show()
                    return@setOnClickListener
                }
                ServerConfig.setStudioUrl(this@ServerConfigActivity, url)
                finish()
            }
        }

        root.addView(label)
        root.addView(input)
        root.addView(save)
        setContentView(root)

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
