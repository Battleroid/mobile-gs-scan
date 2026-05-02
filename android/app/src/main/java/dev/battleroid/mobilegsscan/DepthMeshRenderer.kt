package dev.battleroid.mobilegsscan

import android.media.Image
import android.opengl.GLES20
import android.opengl.Matrix
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.nio.FloatBuffer
import java.nio.ShortBuffer

/**
 * Phase 3 prototype: depth-mesh AR overlay.
 *
 * Per ARCore frame, acquires the camera-aligned depth image,
 * unprojects each (sub-sampled) pixel into world space using the
 * camera's pose at that moment, then renders the resulting mesh
 * as filled translucent triangles. The mesh is REPLACED on every
 * frame — i.e. you always see "what the depth camera saw most
 * recently, anchored where the camera was at that moment". When
 * you pan the phone, the previous frame's mesh is gone.
 *
 * This is intentionally NOT a TSDF / persistent-mesh accumulator;
 * Scaniverse-style "mesh that fills in as you move around" needs a
 * voxel grid + integration step that's a much bigger lift. The
 * prototype gates whether we go that direction.
 *
 * Limitations / known prototype-grade choices:
 *
 *   - Half-resolution sampling (every 2nd depth pixel in each
 *     axis) for ~14k vertices / ~28k triangles at 320×180 depth.
 *     Cuts per-frame CPU work to ~1ms on a Pixel 10 Pro at the
 *     cost of mesh detail.
 *
 *   - Edge culling: skip a triangle when adjacent corners' depths
 *     differ by more than `EDGE_DEPTH_JUMP_M` absolute (default
 *     8 cm). Prevents the "tent" artifact where a triangle would
 *     stretch from a foreground object to background. Tradeoff: at
 *     surface boundaries we lose some valid triangles too.
 *
 *   - Flat color (translucent green). No per-vertex coverage
 *     shading — that requires a voxel hash + per-vertex lookups
 *     which we deliberately skip in v1 so we can evaluate "is the
 *     mesh useful at all" before building voxel infrastructure.
 *
 *   - Depth-camera intrinsics are derived from the color-camera
 *     intrinsics by scaling to depth resolution. ARCore's depth
 *     stream is aligned to the color image; the FOV is the same,
 *     so a linear scale is approximately right. ARCore also
 *     exposes `Frame.transformCoordinates2d` for the precise
 *     mapping but we use the simpler approach here.
 *
 *   - GLES 2.0 only, single-pass, single dynamic VBO + IBO.
 */
class DepthMeshRenderer {
    companion object {
        private const val FLOAT_SIZE = 4
        private const val SHORT_SIZE = 2
        private const val FLOATS_PER_VERTEX = 3 // x y z

        // Sub-sample stride in depth-pixel units. 2 means we read
        // every other depth pixel in each axis (so 1/4 the total).
        // 320×180 → 160×90 grid → 14400 verts, ~28k triangles.
        private const val SAMPLE_STRIDE = 2

        // Skip a triangle if any edge has depth disagreement larger
        // than this (meters). 8 cm is generous enough to keep walls
        // and curved surfaces intact while still cutting "tents"
        // across object boundaries.
        private const val EDGE_DEPTH_JUMP_M = 0.08f

        // Depth values outside this range are treated as invalid.
        // ARCore depth API is reliable from ~0.3m to ~5m on most
        // devices; clamp wider to be safe and let edge-culling
        // handle the long-distance noise.
        private const val MIN_DEPTH_M = 0.15f
        private const val MAX_DEPTH_M = 8.0f

        // Hard caps so a malformed depth image can't blow past
        // pre-allocated buffer sizes. At 640×480 / stride 2 we'd
        // hit ~76800 verts; cap a touch above that.
        private const val MAX_VERTICES = 100_000
        // Two triangles per quad cell, 3 indices per triangle.
        private const val MAX_INDICES = MAX_VERTICES * 6

        private const val DEFAULT_ALPHA = 0.45f

        // Translucent green, looks natural over most camera scenes.
        // RGBA, .a is multiplied by u_Alpha in the fragment shader.
        private val MESH_COLOR = floatArrayOf(0.4f, 1.0f, 0.6f, 1.0f)

        private const val VERTEX_SHADER = """
            uniform mat4 u_ViewProj;
            attribute vec3 a_Position;
            void main() {
                gl_Position = u_ViewProj * vec4(a_Position, 1.0);
            }
        """

        private const val FRAGMENT_SHADER = """
            precision mediump float;
            uniform vec4 u_Color;
            uniform float u_Alpha;
            void main() {
                gl_FragColor = vec4(u_Color.rgb, u_Color.a * u_Alpha);
            }
        """
    }

    /** Stats for the HUD. [triangles] is the latest mesh's triangle count. */
    data class MeshStats(val triangles: Int, val vertices: Int)

    private var program: Int = 0
    private var posAttrib: Int = 0
    private var viewProjUniform: Int = 0
    private var colorUniform: Int = 0
    private var alphaUniform: Int = 0

    private var vbo: Int = 0
    private var ibo: Int = 0
    private var indexCount: Int = 0
    private var vertexCount: Int = 0

    @Volatile private var alpha: Float = DEFAULT_ALPHA

    // Re-used buffers so per-frame mesh build doesn't churn the
    // allocator. Sized to the worst-case grid; the actual upload
    // uses glBufferSubData with the real count.
    private val vertexScratch: FloatBuffer = ByteBuffer
        .allocateDirect(MAX_VERTICES * FLOATS_PER_VERTEX * FLOAT_SIZE)
        .order(ByteOrder.nativeOrder())
        .asFloatBuffer()
    private val indexScratch: ShortBuffer = ByteBuffer
        .allocateDirect(MAX_INDICES * SHORT_SIZE)
        .order(ByteOrder.nativeOrder())
        .asShortBuffer()

    private val viewProjMatrix = FloatArray(16)

    fun createOnGlThread() {
        program = compileProgram()
        posAttrib = GLES20.glGetAttribLocation(program, "a_Position")
        viewProjUniform = GLES20.glGetUniformLocation(program, "u_ViewProj")
        colorUniform = GLES20.glGetUniformLocation(program, "u_Color")
        alphaUniform = GLES20.glGetUniformLocation(program, "u_Alpha")

        val handles = IntArray(2)
        GLES20.glGenBuffers(2, handles, 0)
        vbo = handles[0]
        ibo = handles[1]
        // Reserve worst-case capacity so per-frame uploads can use
        // glBufferSubData and never reallocate.
        GLES20.glBindBuffer(GLES20.GL_ARRAY_BUFFER, vbo)
        GLES20.glBufferData(
            GLES20.GL_ARRAY_BUFFER,
            MAX_VERTICES * FLOATS_PER_VERTEX * FLOAT_SIZE,
            null,
            GLES20.GL_DYNAMIC_DRAW,
        )
        GLES20.glBindBuffer(GLES20.GL_ELEMENT_ARRAY_BUFFER, ibo)
        GLES20.glBufferData(
            GLES20.GL_ELEMENT_ARRAY_BUFFER,
            MAX_INDICES * SHORT_SIZE,
            null,
            GLES20.GL_DYNAMIC_DRAW,
        )
        GLES20.glBindBuffer(GLES20.GL_ARRAY_BUFFER, 0)
        GLES20.glBindBuffer(GLES20.GL_ELEMENT_ARRAY_BUFFER, 0)
    }

    /**
     * Set the global per-fragment alpha multiplier in [0, 1]. Read
     * once per draw; safe from any thread.
     */
    fun setAlpha(a: Float) {
        alpha = a.coerceIn(0f, 1f)
    }

    fun stats(): MeshStats = MeshStats(triangles = indexCount / 3, vertices = vertexCount)

    /**
     * Rebuild the mesh from the current depth image.
     *
     * @param depth ARCore's [Image] from `acquireDepthImage16Bits`.
     *              Single-plane DEPTH16 (uint16 millimeters, little-
     *              endian). The caller owns the [Image] lifecycle —
     *              this method does not close it.
     * @param colorIntrinsics Color-camera intrinsics from the same
     *              frame. We scale to depth resolution internally.
     * @param cameraToWorld Column-major 4x4 from
     *              `frame.camera.pose.toMatrix(...)`. Vertices are
     *              built in WORLD space so the mesh stays put when
     *              the camera moves between frames.
     */
    fun update(
        depth: Image,
        colorIntrinsics: Intrinsics,
        cameraToWorld: FloatArray,
    ) {
        if (program == 0) return

        val depthW = depth.width
        val depthH = depth.height
        if (depthW <= 1 || depthH <= 1) {
            indexCount = 0
            vertexCount = 0
            return
        }

        // Scale color intrinsics to depth resolution. ARCore aligns
        // the depth image with the color image, so the FOV matches
        // and a linear scale of fx/fy/cx/cy is approximately right.
        val scaleX = depthW.toFloat() / colorIntrinsics.w.toFloat()
        val scaleY = depthH.toFloat() / colorIntrinsics.h.toFloat()
        val fx = colorIntrinsics.fx * scaleX
        val fy = colorIntrinsics.fy * scaleY
        val cx = colorIntrinsics.cx * scaleX
        val cy = colorIntrinsics.cy * scaleY

        val plane = depth.planes[0]
        val buffer = plane.buffer.order(ByteOrder.nativeOrder()).asShortBuffer()
        val rowStrideBytes = plane.rowStride
        val rowStrideShorts = rowStrideBytes / SHORT_SIZE
        val pixelStrideBytes = plane.pixelStride // expected = 2

        // Pre-allocate two arrays sized to the sub-sampled grid:
        //   - depthsM: per-grid-vertex Z value in meters (or NaN
        //     for invalid). Used for edge-jump comparisons during
        //     index building, so we don't re-read the depth buffer.
        //   - vertexIdx: per-grid-vertex index into the VBO (or -1
        //     if invalid / not emitted). Used so neighbouring cells
        //     can reference the same shared vertex.
        val gridW = (depthW + SAMPLE_STRIDE - 1) / SAMPLE_STRIDE
        val gridH = (depthH + SAMPLE_STRIDE - 1) / SAMPLE_STRIDE
        val gridSize = gridW * gridH
        if (gridSize > MAX_VERTICES) {
            // Defensive: shouldn't happen with our stride + typical
            // depth resolutions, but better to drop the frame than
            // blow the VBO.
            indexCount = 0
            vertexCount = 0
            return
        }
        val depthsM = FloatArray(gridSize)
        val vertexIdx = IntArray(gridSize) { -1 }

        // Pose-multiplied position storage: we batch the matrix-mul
        // into the inner loop. Reused per-vertex; reading from the
        // vertexScratch FloatBuffer to avoid the allocator.
        vertexScratch.position(0)
        var vCount = 0

        // Pass 1: emit valid vertices.
        var gy = 0
        var py = 0
        while (py < depthH) {
            var gx = 0
            var px = 0
            while (px < depthW) {
                val rawShort = buffer.get(py * rowStrideShorts + px * (pixelStrideBytes / SHORT_SIZE))
                val depthMm = rawShort.toInt() and 0xFFFF
                val depthM = depthMm * 0.001f
                val gIdx = gy * gridW + gx
                if (depthM < MIN_DEPTH_M || depthM > MAX_DEPTH_M) {
                    depthsM[gIdx] = Float.NaN
                } else {
                    depthsM[gIdx] = depthM
                    if (vCount < MAX_VERTICES) {
                        // Camera-space ray for this depth pixel.
                        // ARCore image y-axis points DOWN, so we
                        // negate cy to flip into the OpenGL -Y up
                        // convention used by the camera pose
                        // matrix. -Z is forward (camera looks
                        // along -Z in OpenGL).
                        val camX = (px - cx) * depthM / fx
                        val camY = -(py - cy) * depthM / fy
                        val camZ = -depthM
                        // Multiply pose * (camX, camY, camZ, 1).
                        // Pose is column-major: m[0..3] = col0 etc.
                        val m = cameraToWorld
                        val wx = m[0] * camX + m[4] * camY + m[8] * camZ + m[12]
                        val wy = m[1] * camX + m[5] * camY + m[9] * camZ + m[13]
                        val wz = m[2] * camX + m[6] * camY + m[10] * camZ + m[14]
                        vertexScratch.put(wx)
                        vertexScratch.put(wy)
                        vertexScratch.put(wz)
                        vertexIdx[gIdx] = vCount
                        vCount += 1
                    }
                }
                gx += 1
                px += SAMPLE_STRIDE
            }
            gy += 1
            py += SAMPLE_STRIDE
        }
        vertexScratch.position(0)

        // Pass 2: emit triangle indices for each grid quad whose
        // four corners are all valid AND no edge spans more than
        // EDGE_DEPTH_JUMP_M. Each quad → two triangles.
        indexScratch.position(0)
        var iCount = 0
        for (cy0 in 0 until gridH - 1) {
            for (cx0 in 0 until gridW - 1) {
                val tl = cy0 * gridW + cx0
                val tr = tl + 1
                val bl = tl + gridW
                val br = bl + 1
                val dtl = depthsM[tl]; val dtr = depthsM[tr]
                val dbl = depthsM[bl]; val dbr = depthsM[br]
                if (dtl.isNaN() || dtr.isNaN() || dbl.isNaN() || dbr.isNaN()) continue
                // Edge thresholds (4 sides + 1 diagonal). Worst-case
                // span of all six pairs gates the quad.
                val maxJump = maxOf(
                    kotlin.math.abs(dtl - dtr),
                    kotlin.math.abs(dtl - dbl),
                    kotlin.math.abs(dtr - dbr),
                    kotlin.math.abs(dbl - dbr),
                    kotlin.math.abs(dtl - dbr),
                    kotlin.math.abs(dtr - dbl),
                )
                if (maxJump > EDGE_DEPTH_JUMP_M) continue
                val itl = vertexIdx[tl]; val itr = vertexIdx[tr]
                val ibl = vertexIdx[bl]; val ibr = vertexIdx[br]
                if (itl < 0 || itr < 0 || ibl < 0 || ibr < 0) continue
                if (iCount + 6 > MAX_INDICES) break
                // Two triangles: (tl, bl, br) and (tl, br, tr).
                // Winding order doesn't matter — we don't enable
                // back-face culling on the overlay, but consistency
                // makes any future culling work.
                indexScratch.put(itl.toShort())
                indexScratch.put(ibl.toShort())
                indexScratch.put(ibr.toShort())
                indexScratch.put(itl.toShort())
                indexScratch.put(ibr.toShort())
                indexScratch.put(itr.toShort())
                iCount += 6
            }
            if (iCount + 6 > MAX_INDICES) break
        }
        indexScratch.position(0)

        // Upload both buffers. glBufferSubData reuses the
        // already-allocated DYNAMIC_DRAW storage from createOnGlThread.
        GLES20.glBindBuffer(GLES20.GL_ARRAY_BUFFER, vbo)
        GLES20.glBufferSubData(
            GLES20.GL_ARRAY_BUFFER,
            0,
            vCount * FLOATS_PER_VERTEX * FLOAT_SIZE,
            vertexScratch,
        )
        GLES20.glBindBuffer(GLES20.GL_ELEMENT_ARRAY_BUFFER, ibo)
        GLES20.glBufferSubData(
            GLES20.GL_ELEMENT_ARRAY_BUFFER,
            0,
            iCount * SHORT_SIZE,
            indexScratch,
        )
        GLES20.glBindBuffer(GLES20.GL_ARRAY_BUFFER, 0)
        GLES20.glBindBuffer(GLES20.GL_ELEMENT_ARRAY_BUFFER, 0)

        vertexCount = vCount
        indexCount = iCount
    }

    fun draw(viewMatrix: FloatArray, projMatrix: FloatArray) {
        if (program == 0) return
        if (indexCount == 0) return

        Matrix.multiplyMM(viewProjMatrix, 0, projMatrix, 0, viewMatrix, 0)

        GLES20.glUseProgram(program)
        GLES20.glUniformMatrix4fv(viewProjUniform, 1, false, viewProjMatrix, 0)
        GLES20.glUniform4fv(colorUniform, 1, MESH_COLOR, 0)
        GLES20.glUniform1f(alphaUniform, alpha)

        // Depth test on so the mesh self-occludes properly. The
        // camera quad doesn't write depth, so the mesh effectively
        // floats over the camera image with internal depth ordering
        // among triangles.
        GLES20.glEnable(GLES20.GL_DEPTH_TEST)
        GLES20.glDepthMask(true)

        // Standard alpha blend so we get the translucent veil look
        // and the camera image shows through.
        GLES20.glEnable(GLES20.GL_BLEND)
        GLES20.glBlendFunc(GLES20.GL_SRC_ALPHA, GLES20.GL_ONE_MINUS_SRC_ALPHA)

        GLES20.glBindBuffer(GLES20.GL_ARRAY_BUFFER, vbo)
        GLES20.glVertexAttribPointer(
            posAttrib, 3, GLES20.GL_FLOAT, false, FLOATS_PER_VERTEX * FLOAT_SIZE, 0,
        )
        GLES20.glEnableVertexAttribArray(posAttrib)

        GLES20.glBindBuffer(GLES20.GL_ELEMENT_ARRAY_BUFFER, ibo)
        GLES20.glDrawElements(
            GLES20.GL_TRIANGLES,
            indexCount,
            GLES20.GL_UNSIGNED_SHORT,
            0,
        )

        GLES20.glDisableVertexAttribArray(posAttrib)
        GLES20.glBindBuffer(GLES20.GL_ARRAY_BUFFER, 0)
        GLES20.glBindBuffer(GLES20.GL_ELEMENT_ARRAY_BUFFER, 0)
        GLES20.glDisable(GLES20.GL_BLEND)
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
            "DepthMeshRenderer link failed: " + GLES20.glGetProgramInfoLog(prog)
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
            "DepthMeshRenderer shader compile failed: " + GLES20.glGetShaderInfoLog(shader)
        }
        return shader
    }
}
