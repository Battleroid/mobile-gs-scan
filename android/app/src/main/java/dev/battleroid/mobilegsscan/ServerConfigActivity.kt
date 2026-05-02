package dev.battleroid.mobilegsscan

import android.os.Bundle
import android.text.InputType
import android.view.Gravity
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.SeekBar
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

        val urlLabel = TextView(this).apply {
            text = getString(R.string.label_studio_url)
        }
        val urlInput = EditText(this).apply {
            hint = getString(R.string.hint_studio_url)
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_URI
            setText(ServerConfig.studioUrl(this@ServerConfigActivity).orEmpty())
        }
        val urlHelper = mutedHelper(getString(R.string.hint_studio_url_help))

        // ── capture-rate slider ─────────────────────────────────
        val fpsSection = sectionHeader(getString(R.string.label_capture_fps))
        val fpsValueLabel = TextView(this).apply {
            text = getString(
                R.string.capture_fps_fmt,
                ServerConfig.captureFps(this@ServerConfigActivity),
            )
            setTextColor(getColor(R.color.fg))
        }
        val fpsSlider = SeekBar(this).apply {
            min = ServerConfig.MIN_FPS
            max = ServerConfig.MAX_FPS
            progress = ServerConfig.captureFps(this@ServerConfigActivity)
            setOnSeekBarChangeListener(object : SeekBar.OnSeekBarChangeListener {
                override fun onProgressChanged(sb: SeekBar, value: Int, fromUser: Boolean) {
                    val fps = value.coerceAtLeast(ServerConfig.MIN_FPS)
                    fpsValueLabel.text = getString(R.string.capture_fps_fmt, fps)
                }
                override fun onStartTrackingTouch(sb: SeekBar) {}
                override fun onStopTrackingTouch(sb: SeekBar) {}
            })
        }
        val fpsHelper = mutedHelper(getString(R.string.hint_capture_fps))

        // ── jpeg-quality slider ─────────────────────────────────
        val qSection = sectionHeader(getString(R.string.label_capture_jpeg_quality))
        val qValueLabel = TextView(this).apply {
            text = getString(
                R.string.capture_jpeg_quality_fmt,
                ServerConfig.captureJpegQuality(this@ServerConfigActivity),
            )
            setTextColor(getColor(R.color.fg))
        }
        val qSlider = SeekBar(this).apply {
            min = ServerConfig.MIN_JPEG_QUALITY
            max = ServerConfig.MAX_JPEG_QUALITY
            progress = ServerConfig.captureJpegQuality(this@ServerConfigActivity)
            setOnSeekBarChangeListener(object : SeekBar.OnSeekBarChangeListener {
                override fun onProgressChanged(sb: SeekBar, value: Int, fromUser: Boolean) {
                    val q = value.coerceAtLeast(ServerConfig.MIN_JPEG_QUALITY)
                    qValueLabel.text = getString(R.string.capture_jpeg_quality_fmt, q)
                }
                override fun onStartTrackingTouch(sb: SeekBar) {}
                override fun onStopTrackingTouch(sb: SeekBar) {}
            })
        }
        val qHelper = mutedHelper(getString(R.string.hint_capture_jpeg_quality))

        // ── training fidelity preset ────────────────────────────
        // Three buttons: Low (5k iters / ~3 min), Standard (15k /
        // ~10 min, app default), High (30k / ~25 min). The selected
        // preset is highlighted via alpha; tapping a button selects
        // and deselects the others. Persisted on Save.
        val tiSection = sectionHeader(getString(R.string.label_train_fidelity))
        var selectedIters = ServerConfig.captureTrainIters(this)
        val tiValueLabel = TextView(this).apply {
            text = getString(R.string.selected_train_iters_fmt, selectedIters)
            setTextColor(getColor(R.color.fg))
        }
        val btnLow = Button(this).apply {
            text = getString(R.string.preset_train_low)
        }
        val btnStandard = Button(this).apply {
            text = getString(R.string.preset_train_standard)
        }
        val btnHigh = Button(this).apply {
            text = getString(R.string.preset_train_high)
        }
        val tiButtons = listOf(
            btnLow to ServerConfig.TRAIN_ITERS_LOW,
            btnStandard to ServerConfig.TRAIN_ITERS_STANDARD,
            btnHigh to ServerConfig.TRAIN_ITERS_HIGH,
        )
        fun refreshPresetSelection() {
            tiButtons.forEach { (btn, value) ->
                btn.alpha = if (value == selectedIters) 1.0f else 0.55f
            }
        }
        tiButtons.forEach { (btn, value) ->
            btn.setOnClickListener {
                selectedIters = value
                tiValueLabel.text = getString(R.string.selected_train_iters_fmt, selectedIters)
                refreshPresetSelection()
            }
        }
        refreshPresetSelection()
        val presetRow = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
        }
        val gap = (8 * resources.displayMetrics.density).toInt()
        val one = LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f)
        val withMarginEnd = LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f).apply {
            marginEnd = gap
        }
        presetRow.addView(btnLow, withMarginEnd)
        presetRow.addView(btnStandard, withMarginEnd)
        presetRow.addView(btnHigh, one)
        val tiHelper = mutedHelper(getString(R.string.hint_train_fidelity))

        // ── coverage-overlay opacity slider ─────────────────────
        // Multiplies the per-fragment alpha in CoverageRenderer's
        // shader. 20..100% so the user can't accidentally make the
        // overlay invisible and think it's broken; default 70%.
        val aSection = sectionHeader(getString(R.string.label_overlay_alpha))
        val aValueLabel = TextView(this).apply {
            text = getString(
                R.string.overlay_alpha_fmt,
                ServerConfig.coverageOverlayAlphaPct(this@ServerConfigActivity),
            )
            setTextColor(getColor(R.color.fg))
        }
        val aSlider = SeekBar(this).apply {
            min = ServerConfig.MIN_OVERLAY_ALPHA_PCT
            max = ServerConfig.MAX_OVERLAY_ALPHA_PCT
            progress = ServerConfig.coverageOverlayAlphaPct(this@ServerConfigActivity)
            setOnSeekBarChangeListener(object : SeekBar.OnSeekBarChangeListener {
                override fun onProgressChanged(sb: SeekBar, value: Int, fromUser: Boolean) {
                    val a = value.coerceAtLeast(ServerConfig.MIN_OVERLAY_ALPHA_PCT)
                    aValueLabel.text = getString(R.string.overlay_alpha_fmt, a)
                }
                override fun onStartTrackingTouch(sb: SeekBar) {}
                override fun onStopTrackingTouch(sb: SeekBar) {}
            })
        }
        val aHelper = mutedHelper(getString(R.string.hint_overlay_alpha))

        // ── save ──────────────────────────────────────────────
        val save = Button(this).apply {
            text = getString(R.string.action_save)
            setOnClickListener {
                val raw = urlInput.text.toString().trim()
                if (raw.isEmpty()) {
                    Toast.makeText(
                        this@ServerConfigActivity,
                        "studio URL required",
                        Toast.LENGTH_SHORT,
                    ).show()
                    return@setOnClickListener
                }
                ServerConfig.setStudioUrl(this@ServerConfigActivity, raw)
                ServerConfig.setCaptureFps(this@ServerConfigActivity, fpsSlider.progress)
                ServerConfig.setCaptureJpegQuality(
                    this@ServerConfigActivity, qSlider.progress,
                )
                ServerConfig.setCaptureTrainIters(
                    this@ServerConfigActivity, selectedIters,
                )
                ServerConfig.setCoverageOverlayAlphaPct(
                    this@ServerConfigActivity, aSlider.progress,
                )
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

        root.addView(urlLabel)
        root.addView(urlInput)
        root.addView(urlHelper)
        root.addView(fpsSection)
        root.addView(fpsValueLabel)
        root.addView(fpsSlider)
        root.addView(fpsHelper)
        root.addView(qSection)
        root.addView(qValueLabel)
        root.addView(qSlider)
        root.addView(qHelper)
        root.addView(tiSection)
        root.addView(tiValueLabel)
        root.addView(presetRow)
        root.addView(tiHelper)
        root.addView(aSection)
        root.addView(aValueLabel)
        root.addView(aSlider)
        root.addView(aHelper)
        root.addView(save)
        setContentView(root)

        // Apply the helper-text margins after addView so the layout
        // params are the LinearLayout-owned ones (they're
        // ViewGroup.LayoutParams during the apply{} call above and
        // don't have topMargin / bottomMargin fields).
        listOf(urlHelper, fpsHelper, qHelper, tiHelper, aHelper).forEach { v ->
            (v.layoutParams as LinearLayout.LayoutParams).apply {
                topMargin = (4 * resources.displayMetrics.density).toInt()
                bottomMargin = (16 * resources.displayMetrics.density).toInt()
                v.layoutParams = this
            }
        }
        listOf(fpsSection, qSection, tiSection, aSection).forEach { v ->
            (v.layoutParams as LinearLayout.LayoutParams).apply {
                topMargin = (16 * resources.displayMetrics.density).toInt()
                v.layoutParams = this
            }
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

    private fun mutedHelper(text: String): TextView = TextView(this).apply {
        this.text = text
        textSize = 12f
        setTextColor(getColor(R.color.muted))
        gravity = Gravity.START
    }

    private fun sectionHeader(text: String): TextView = TextView(this).apply {
        this.text = text
        textSize = 14f
        setTextColor(getColor(R.color.fg))
    }
}
