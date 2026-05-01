package dev.battleroid.mobilegsscan

import android.opengl.GLES11Ext
import android.opengl.GLES20
import com.google.ar.core.Coordinates2d
import com.google.ar.core.Frame
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.nio.FloatBuffer

/**
 * Minimal renderer for ARCore's camera-feed external-OES texture.
 *
 * Draws a fullscreen quad textured with the camera image so the
 * GLSurfaceView shows what the phone sees. Modeled on the
 * BackgroundRenderer from the ARCore SDK samples but pared down to
 * the absolute minimum — no depth, no occlusion, no filtering tweaks.
 *
 * Lifecycle:
 *   1. createOnGlThread() — must run on the GL thread (i.e. inside
 *      GLSurfaceView.Renderer.onSurfaceCreated). Allocates the
 *      EXT_OES texture id, compiles + links the shader program.
 *   2. ARCaptureSession.setTextureName(textureId) — binds the same
 *      OES texture to ARCore's session so session.update() writes
 *      the camera frame into it.
 *   3. updateTexCoords(frame) — every frame, ask ARCore for the
 *      view→texture transform so the camera image lands the right
 *      way up regardless of device orientation.
 *   4. draw() — every frame, paint the quad using the latest
 *      texture coordinates.
 */
class BackgroundRenderer {
    companion object {
        private const val COORDS_PER_VERTEX = 2
        private const val TEXCOORDS_PER_VERTEX = 2
        private const val FLOAT_SIZE = 4

        // Fullscreen quad in normalized-device coordinates: two
        // triangles covering the whole viewport via TRIANGLE_STRIP.
        private val QUAD_COORDS = floatArrayOf(
            -1f, -1f,
            +1f, -1f,
            -1f, +1f,
            +1f, +1f,
        )

        private const val VERTEX_SHADER = """
            attribute vec4 a_Position;
            attribute vec2 a_TexCoord;
            varying vec2 v_TexCoord;
            void main() {
                gl_Position = a_Position;
                v_TexCoord = a_TexCoord;
            }
        """

        // samplerExternalOES is the bridge between an EXT_OES texture
        // (which is what ARCore writes camera frames into) and a
        // standard fragment shader sample. The #extension pragma is
        // mandatory — without it the shader fails to compile.
        private const val FRAGMENT_SHADER = """
            #extension GL_OES_EGL_image_external : require
            precision mediump float;
            uniform samplerExternalOES u_Texture;
            varying vec2 v_TexCoord;
            void main() {
                gl_FragColor = texture2D(u_Texture, v_TexCoord);
            }
        """
    }

    private var program: Int = 0
    private var positionAttrib: Int = 0
    private var texCoordAttrib: Int = 0
    private var textureUniform: Int = 0

    private val quadCoords: FloatBuffer = ByteBuffer
        .allocateDirect(QUAD_COORDS.size * FLOAT_SIZE)
        .order(ByteOrder.nativeOrder())
        .asFloatBuffer()
        .apply { put(QUAD_COORDS); position(0) }

    // Receives the per-frame transform from ARCore. Same length as
    // quadCoords (4 verts * 2 components) but contents are recomputed
    // each frame whenever the display geometry changes.
    private val quadTexCoords: FloatBuffer = ByteBuffer
        .allocateDirect(QUAD_COORDS.size * FLOAT_SIZE)
        .order(ByteOrder.nativeOrder())
        .asFloatBuffer()

    /** -1 until createOnGlThread() runs. Pass to ARCore's
     *  Session.setCameraTextureName once it's ≥ 0. */
    var textureId: Int = -1
        private set

    fun createOnGlThread() {
        val handles = IntArray(1)
        GLES20.glGenTextures(1, handles, 0)
        textureId = handles[0]
        GLES20.glBindTexture(GLES11Ext.GL_TEXTURE_EXTERNAL_OES, textureId)
        GLES20.glTexParameteri(
            GLES11Ext.GL_TEXTURE_EXTERNAL_OES,
            GLES20.GL_TEXTURE_WRAP_S,
            GLES20.GL_CLAMP_TO_EDGE,
        )
        GLES20.glTexParameteri(
            GLES11Ext.GL_TEXTURE_EXTERNAL_OES,
            GLES20.GL_TEXTURE_WRAP_T,
            GLES20.GL_CLAMP_TO_EDGE,
        )
        GLES20.glTexParameteri(
            GLES11Ext.GL_TEXTURE_EXTERNAL_OES,
            GLES20.GL_TEXTURE_MIN_FILTER,
            GLES20.GL_LINEAR,
        )
        GLES20.glTexParameteri(
            GLES11Ext.GL_TEXTURE_EXTERNAL_OES,
            GLES20.GL_TEXTURE_MAG_FILTER,
            GLES20.GL_LINEAR,
        )

        program = compileProgram()
        positionAttrib = GLES20.glGetAttribLocation(program, "a_Position")
        texCoordAttrib = GLES20.glGetAttribLocation(program, "a_TexCoord")
        textureUniform = GLES20.glGetUniformLocation(program, "u_Texture")
    }

    /**
     * Refresh the per-frame texture-coordinate buffer using ARCore's
     * view→texture transform. Skipped when display geometry is
     * unchanged — the transform only flips on rotation / size change.
     */
    fun updateTexCoords(frame: Frame) {
        if (!frame.hasDisplayGeometryChanged() && quadTexCoords.position() != 0) return
        frame.transformCoordinates2d(
            Coordinates2d.OPENGL_NORMALIZED_DEVICE_COORDINATES,
            quadCoords,
            Coordinates2d.TEXTURE_NORMALIZED,
            quadTexCoords,
        )
        // transformCoordinates2d leaves position() at the end; reset
        // so the subsequent glVertexAttribPointer reads from index 0.
        quadTexCoords.position(0)
    }

    fun draw() {
        if (textureId < 0 || program == 0) return
        // Camera background is opaque — disable depth so it doesn't
        // self-occlude with overlay geometry drawn on top.
        GLES20.glDisable(GLES20.GL_DEPTH_TEST)
        GLES20.glDepthMask(false)
        GLES20.glUseProgram(program)

        GLES20.glActiveTexture(GLES20.GL_TEXTURE0)
        GLES20.glBindTexture(GLES11Ext.GL_TEXTURE_EXTERNAL_OES, textureId)
        GLES20.glUniform1i(textureUniform, 0)

        quadCoords.position(0)
        GLES20.glVertexAttribPointer(
            positionAttrib, COORDS_PER_VERTEX, GLES20.GL_FLOAT, false, 0, quadCoords,
        )
        GLES20.glEnableVertexAttribArray(positionAttrib)

        quadTexCoords.position(0)
        GLES20.glVertexAttribPointer(
            texCoordAttrib, TEXCOORDS_PER_VERTEX, GLES20.GL_FLOAT, false, 0, quadTexCoords,
        )
        GLES20.glEnableVertexAttribArray(texCoordAttrib)

        GLES20.glDrawArrays(GLES20.GL_TRIANGLE_STRIP, 0, 4)

        GLES20.glDisableVertexAttribArray(positionAttrib)
        GLES20.glDisableVertexAttribArray(texCoordAttrib)
        GLES20.glDepthMask(true)
        GLES20.glEnable(GLES20.GL_DEPTH_TEST)
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
            "BackgroundRenderer link failed: " + GLES20.glGetProgramInfoLog(prog)
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
            "BackgroundRenderer shader compile failed: " + GLES20.glGetShaderInfoLog(shader)
        }
        return shader
    }
}
