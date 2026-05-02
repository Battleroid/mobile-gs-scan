package dev.battleroid.mobilegsscan

import android.opengl.GLES20
import android.opengl.Matrix
import com.google.ar.core.PointCloud
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.nio.FloatBuffer

/**
 * Scaniverse-style AR coverage overlay.
 *
 * Renders ARCore's tracked feature point cloud as 3D dots ON THE
 * ACTUAL SUBJECT SURFACE (not floating in mid-air at past camera
 * positions), colored by how many captured frames have observed
 * each point:
 *
 *   - 1 observation     → red    (just-seen, sparse)
 *   - 2..4 observations → yellow (moderate)
 *   - ≥5 observations    → green  (well-covered)
 *
 * The user can see at a glance which surface regions are sparsely
 * sampled (red) vs richly sampled (green) and aim there next. A
 * companion HUD on the activity reports total point count + the
 * percentage of points considered well-covered (≥3 observations).
 *
 * Three depth cues stack in the rendering pipeline so the eye
 * reads the overlay as 3D rather than as a flat sprinkle of dots
 * over the camera image:
 *
 *   1. Distance-attenuated point size in the vertex shader:
 *      `gl_PointSize = clamp(BASE / max(w, 0.1), 3.0, 28.0)`.
 *      Closer points are bigger; far points stay visible at the
 *      lower clamp.
 *
 *   2. Distance-fade alpha in the fragment shader:
 *      `1 / (1 + d / FADE_K_M)`. Closer points keep their full
 *      colour; far ones recede toward translucent.
 *
 *   3. Round point sprites: the fragment shader discards
 *      fragments outside the unit circle within `gl_PointCoord`
 *      and softens the edge with a smoothstep. GL_POINTS draws as
 *      square sprites by default; the discard-and-feather makes
 *      them look like crisp circles.
 *
 * Lifecycle:
 *   1. createOnGlThread() — must run on the GL thread (i.e. inside
 *      GLSurfaceView.Renderer.onSurfaceCreated). Allocates the
 *      shader program + a single dynamic VBO.
 *   2. recordObservations(pointCloud) — called once per CAPTURED
 *      frame (i.e. inside the captureGateActive branch after
 *      pollFrameData returns), increments per-point observation
 *      counts and refreshes the latest world-space position. Marks
 *      the VBO dirty.
 *   3. draw(viewMatrix, projMatrix) — called every render frame
 *      after the camera background is drawn. Rebuilds the VBO from
 *      the observation map if dirty, then renders GL_POINTS with
 *      depth test enabled and alpha blending so the camera image
 *      shows through translucent dots.
 *
 * Single GL VBO + a flat hashmap: at the typical few-thousand-point
 * scale ARCore returns, the per-frame rebuild is comfortably under
 * a millisecond on a 4090-tethered Pixel 10 Pro.
 */
class CoverageRenderer {
    companion object {
        private const val FLOAT_SIZE = 4
        private const val FLOATS_PER_VERTEX = 7  // x y z + r g b a
        private const val STRIDE = FLOATS_PER_VERTEX * FLOAT_SIZE

        // Cap on points uploaded to the GPU per frame. ARCore
        // typically tracks a few hundred to ~2k points; this is a
        // safety net if a high-feature scene blows past that.
        private const val MAX_POINTS = 8192

        // "Well-covered" threshold for the HUD percentage. 3 hits
        // empirically corresponds to enough multi-view coverage
        // for splatfacto to triangulate a reasonable gaussian.
        private const val WELL_COVERED_THRESHOLD = 3

        // Color buckets. RGBA, all alpha=1; the global u_Alpha
        // uniform multiplies in the fragment shader so the user
        // can tune overall translucency from Settings without
        // touching the per-bucket palette.
        private val SPARSE = floatArrayOf(1.0f, 0.4f, 0.4f, 1.0f)    // red
        private val MODERATE = floatArrayOf(1.0f, 1.0f, 0.4f, 1.0f)  // yellow
        private val COVERED = floatArrayOf(0.4f, 1.0f, 0.5f, 1.0f)   // green

        // Base on-screen point size in pixels at unit camera
        // distance; shader divides by view-space depth so closer
        // points are bigger. Bumped up from 30 → 50 to give close
        // points a visibly larger footprint at typical capture
        // distances (under ~2m the previous size was clamped at
        // 16 px and lost any close-vs-very-close size signal).
        private const val POINT_BASE_SIZE = 50.0f

        // Per-point screen-size clamp, in pixels. The lower bound
        // keeps far points readable on hi-DPI Pixel screens (was
        // 2 px which read as a single anti-aliased pixel and
        // disappeared against busy backgrounds). The upper bound
        // keeps the closest points from ballooning into giant
        // blobs when the camera is right on top of a surface.
        private const val POINT_MIN_SIZE = 3.0f
        private const val POINT_MAX_SIZE = 28.0f

        // Default alpha if the activity never calls setAlpha. 0.7
        // matches the user-configurable default in ServerConfig
        // (DEFAULT_OVERLAY_ALPHA_PCT = 70).
        private const val DEFAULT_ALPHA = 0.7f

        // Falloff distance for the depth-fade curve (metres). The
        // fragment shader scales alpha by `1 / (1 + d / FADE_K_M)`
        // where d is the vertex's clip-space w (≈ view-space
        // depth). 2 m means a point 2 m from the camera gets 50%
        // of full alpha; at 6 m it's 25%; at 10 m it's 17%. Tuned
        // to keep close near-field dots crisp while letting the
        // far-field background "wash out" so the user can see the
        // 3D structure of where they've covered.
        private const val DEPTH_FADE_K_M = 2.0f

        private const val VERTEX_SHADER = """
            uniform mat4 u_ViewProj;
            uniform float u_BaseSize;
            uniform float u_MinSize;
            uniform float u_MaxSize;
            attribute vec3 a_Position;
            attribute vec4 a_Color;
            varying vec4 v_Color;
            varying float v_Depth;
            void main() {
                gl_Position = u_ViewProj * vec4(a_Position, 1.0);
                gl_PointSize = clamp(u_BaseSize / max(gl_Position.w, 0.1), u_MinSize, u_MaxSize);
                v_Color = a_Color;
                // gl_Position.w after the perspective transform is
                // approximately the view-space depth in metres,
                // which is what we want for the depth-fade curve.
                v_Depth = gl_Position.w;
            }
        """

        private const val FRAGMENT_SHADER = """
            precision mediump float;
            uniform float u_Alpha;
            uniform float u_FadeK;
            varying vec4 v_Color;
            varying float v_Depth;
            void main() {
                // Round point sprites: gl_PointCoord runs 0..1
                // across the square. Discard fragments outside the
                // inscribed unit circle (radius 0.5 from centre)
                // and feather a couple of pixels at the edge for
                // cheap anti-aliasing.
                vec2 uv = gl_PointCoord - vec2(0.5);
                float r = length(uv);
                if (r > 0.5) discard;
                float edgeAa = 1.0 - smoothstep(0.42, 0.5, r);

                // Standard depth-cued opacity curve: bright at the
                // near plane, asymptotically fading toward zero as
                // the point recedes. Without this the overlay
                // reads as a uniform sprinkle of dots over the
                // camera image with no 3D cue.
                float fade = 1.0 / (1.0 + v_Depth / u_FadeK);
                gl_FragColor = vec4(
                    v_Color.rgb,
                    v_Color.a * u_Alpha * fade * edgeAa
                );
            }
        """
    }

    /** Snapshot of coverage stats for the activity's HUD. */
    data class CoverageStats(val totalPoints: Int, val wellCoveredPct: Int)

    private var program: Int = 0
    private var posAttrib: Int = 0
    private var colorAttrib: Int = 0
    private var viewProjUniform: Int = 0
    private var baseSizeUniform: Int = 0
    private var minSizeUniform: Int = 0
    private var maxSizeUniform: Int = 0
    private var alphaUniform: Int = 0
    private var fadeKUniform: Int = 0

    private var vbo: Int = 0
    private var pointCount: Int = 0
    private var dirty: Boolean = false

    @Volatile private var alpha: Float = DEFAULT_ALPHA

    // ARCore-stable point id → cumulative observation count.
    private val observations = HashMap<Long, Int>()
    // ARCore-stable point id → latest world-space xyz. ARCore can
    // shift a point's position slightly across frames as it refines
    // its estimate; we always render the latest report.
    private val positions = HashMap<Long, FloatArray>()

    private val viewProjMatrix = FloatArray(16)

    fun createOnGlThread() {
        program = compileProgram()
        posAttrib = GLES20.glGetAttribLocation(program, "a_Position")
        colorAttrib = GLES20.glGetAttribLocation(program, "a_Color")
        viewProjUniform = GLES20.glGetUniformLocation(program, "u_ViewProj")
        baseSizeUniform = GLES20.glGetUniformLocation(program, "u_BaseSize")
        minSizeUniform = GLES20.glGetUniformLocation(program, "u_MinSize")
        maxSizeUniform = GLES20.glGetUniformLocation(program, "u_MaxSize")
        alphaUniform = GLES20.glGetUniformLocation(program, "u_Alpha")
        fadeKUniform = GLES20.glGetUniformLocation(program, "u_FadeK")

        val handles = IntArray(1)
        GLES20.glGenBuffers(1, handles, 0)
        vbo = handles[0]
        // Reserve full capacity up front; per-frame uploads use
        // glBufferSubData so we never reallocate.
        GLES20.glBindBuffer(GLES20.GL_ARRAY_BUFFER, vbo)
        GLES20.glBufferData(
            GLES20.GL_ARRAY_BUFFER,
            MAX_POINTS * STRIDE,
            null,
            GLES20.GL_DYNAMIC_DRAW,
        )
        GLES20.glBindBuffer(GLES20.GL_ARRAY_BUFFER, 0)
    }

    /**
     * Set the global per-fragment alpha multiplier in [0, 1]. Safe
     * to call from any thread; the value is read once per draw
     * call. Out-of-range values are clamped.
     */
    fun setAlpha(a: Float) {
        alpha = a.coerceIn(0f, 1f)
    }

    /**
     * Record one observation of every point currently in the
     * supplied [PointCloud]. Caller is responsible for closing the
     * PointCloud after this returns (via Closeable.use).
     *
     * ARCore's PointCloud.points is a FloatBuffer of (x, y, z,
     * confidence) per point; ids is an IntBuffer of stable ids.
     * A given id is stable across frames as long as ARCore keeps
     * tracking that feature; drop in tracking, the id is gone.
     */
    fun recordObservations(pointCloud: PointCloud) {
        val pts = pointCloud.points ?: return
        val ids = pointCloud.ids ?: return
        if (ids.remaining() == 0) return

        pts.rewind()
        ids.rewind()
        val n = ids.remaining()
        var i = 0
        while (i < n && pts.remaining() >= 4) {
            val id = ids.get().toLong()
            val x = pts.get()
            val y = pts.get()
            val z = pts.get()
            pts.get()  // skip confidence
            observations[id] = (observations[id] ?: 0) + 1
            val pos = positions.getOrPut(id) { FloatArray(3) }
            pos[0] = x; pos[1] = y; pos[2] = z
            i++
        }
        dirty = true
    }

    fun draw(viewMatrix: FloatArray, projMatrix: FloatArray) {
        if (program == 0) return
        if (dirty) rebuildVbo()
        if (pointCount == 0) return

        Matrix.multiplyMM(viewProjMatrix, 0, projMatrix, 0, viewMatrix, 0)

        GLES20.glUseProgram(program)
        GLES20.glUniformMatrix4fv(viewProjUniform, 1, false, viewProjMatrix, 0)
        GLES20.glUniform1f(baseSizeUniform, POINT_BASE_SIZE)
        GLES20.glUniform1f(minSizeUniform, POINT_MIN_SIZE)
        GLES20.glUniform1f(maxSizeUniform, POINT_MAX_SIZE)
        GLES20.glUniform1f(alphaUniform, alpha)
        GLES20.glUniform1f(fadeKUniform, DEPTH_FADE_K_M)

        // Depth test on so points behind closer geometry will be
        // occluded once we have closer geometry. The camera quad
        // itself doesn't write depth, so this just establishes a
        // sensible baseline for future overlay layers.
        GLES20.glEnable(GLES20.GL_DEPTH_TEST)
        GLES20.glDepthMask(true)

        // Alpha blending so the camera image shows through the
        // dots. Without GL_BLEND the fragment-color alpha would be
        // ignored and dots would always be opaque regardless of
        // u_Alpha.
        GLES20.glEnable(GLES20.GL_BLEND)
        GLES20.glBlendFunc(GLES20.GL_SRC_ALPHA, GLES20.GL_ONE_MINUS_SRC_ALPHA)

        GLES20.glBindBuffer(GLES20.GL_ARRAY_BUFFER, vbo)
        GLES20.glVertexAttribPointer(
            posAttrib, 3, GLES20.GL_FLOAT, false, STRIDE, 0,
        )
        GLES20.glEnableVertexAttribArray(posAttrib)
        GLES20.glVertexAttribPointer(
            colorAttrib, 4, GLES20.GL_FLOAT, false, STRIDE, 3 * FLOAT_SIZE,
        )
        GLES20.glEnableVertexAttribArray(colorAttrib)

        GLES20.glDrawArrays(GLES20.GL_POINTS, 0, pointCount)

        GLES20.glDisableVertexAttribArray(posAttrib)
        GLES20.glDisableVertexAttribArray(colorAttrib)
        GLES20.glBindBuffer(GLES20.GL_ARRAY_BUFFER, 0)
        // Restore default GL state. Other renderers in the chain
        // assume blending is off unless they enable it themselves.
        GLES20.glDisable(GLES20.GL_BLEND)
    }

    fun coverageStats(): CoverageStats {
        val total = observations.size
        if (total == 0) return CoverageStats(0, 0)
        val wellCovered = observations.values.count { it >= WELL_COVERED_THRESHOLD }
        val pct = ((wellCovered.toFloat() / total) * 100f).toInt().coerceIn(0, 100)
        return CoverageStats(total, pct)
    }

    private fun rebuildVbo(): Unit {
        val ids = observations.keys.toList()
        // Hard-cap at MAX_POINTS. Rare with ARCore's typical
        // tracking density but we'd rather drop a few than blow
        // past our pre-allocated VBO capacity.
        val take = if (ids.size > MAX_POINTS) ids.subList(0, MAX_POINTS) else ids

        val buf: FloatBuffer = ByteBuffer
            .allocateDirect(take.size * STRIDE)
            .order(ByteOrder.nativeOrder())
            .asFloatBuffer()

        for (id in take) {
            val pos = positions[id] ?: continue
            val obs = observations[id] ?: 0
            val color = when {
                obs >= 5 -> COVERED
                obs >= 2 -> MODERATE
                else -> SPARSE
            }
            buf.put(pos[0]); buf.put(pos[1]); buf.put(pos[2])
            buf.put(color[0]); buf.put(color[1]); buf.put(color[2]); buf.put(color[3])
        }
        buf.position(0)

        pointCount = take.size
        GLES20.glBindBuffer(GLES20.GL_ARRAY_BUFFER, vbo)
        GLES20.glBufferSubData(
            GLES20.GL_ARRAY_BUFFER,
            0,
            take.size * STRIDE,
            buf,
        )
        GLES20.glBindBuffer(GLES20.GL_ARRAY_BUFFER, 0)
        dirty = false
    }

    private fun compileProgram(): Int {
        val vs = compileShader(GLES20.GL_VERTEX_SHADER, VERTEX_SHADER)
        val fs = compileShader(GLES20.GL_FRAGMENT_SHADER, FRAGMENT_SHADER)
        val prog = GLES20.glCreateProgram()
        GLES20.glAttachShader(prog, vs)
        GLES20.glAttachShader(prog, fs)
        GLES20.glLinkProgram(prog)
        val linked = IntArray(1)
        GLES20.glGetProgramiv(prog, GLES20.GL_LINK_STATUS, linked, 0)
        check(linked[0] != 0) {
            "CoverageRenderer link failed: " + GLES20.glGetProgramInfoLog(prog)
        }
        return prog
    }

    private fun compileShader(type: Int, src: String): Int {
        val shader = GLES20.glCreateShader(type)
        GLES20.glShaderSource(shader, src)
        GLES20.glCompileShader(shader)
        val ok = IntArray(1)
        GLES20.glGetShaderiv(shader, GLES20.GL_COMPILE_STATUS, ok, 0)
        check(ok[0] != 0) {
            "CoverageRenderer shader compile failed: " + GLES20.glGetShaderInfoLog(shader)
        }
        return shader
    }
}
