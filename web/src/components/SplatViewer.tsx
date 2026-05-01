"use client";
// Three.js + react-three-fiber wrapper around Spark
// (https://github.com/sparkjsdev/spark, npm: @sparkjsdev/spark).
//
// Spark exposes two THREE.js objects we add to the scene:
//   - SparkRenderer: hooks into the THREE.js render pipeline so
//     splats and regular meshes can intermix.
//   - SplatMesh:     extends THREE.Object3D, loads a .ply / .spz /
//     .splat / .ksplat URL.
//
// We import dynamically because the package touches `window` at
// import time and would crash during Next.js's server-render pass.
import { Suspense, useEffect, useRef, useState } from "react";
import { Canvas, useThree } from "@react-three/fiber";
import { OrbitControls } from "@react-three/drei";
import * as THREE from "three";

interface Props {
  /** /api/scenes/<id>/artifacts/{ply,spz} URL. */
  url: string;
  className?: string;
}

export function SplatViewer({ url, className }: Props) {
  return (
    <div className={className ?? "h-[60vh] w-full bg-black"}>
      <Canvas
        camera={{ position: [3, 2, 3], fov: 50, near: 0.05, far: 500 }}
        gl={{ antialias: false, powerPreference: "high-performance" }}
      >
        <color attach="background" args={["#0b0b0d"]} />
        <Suspense fallback={null}>
          <SplatScene url={url} />
        </Suspense>
        <OrbitControls makeDefault enableDamping target={[0, 0, 0]} />
      </Canvas>
    </div>
  );
}

function SplatScene({ url }: { url: string }) {
  const { gl, scene } = useThree();
  const groupRef = useRef<THREE.Group>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    let spark: THREE.Object3D | null = null;
    let splat: THREE.Object3D | null = null;
    let fallbackPoints: THREE.Points | null = null;

    (async () => {
      try {
        const mod = await import("@sparkjsdev/spark").catch(async () => {
          // Fallback: render the .ply as a coarse point cloud via
          // three's PLYLoader when @sparkjsdev/spark isn't resolvable
          // (e.g. PR #1 may land before npm install runs in the
          // image build). Lets us verify the plumbing.
          const { PLYLoader } = await import(
            "three/examples/jsm/loaders/PLYLoader.js"
          );
          const loader = new PLYLoader();
          const geom = await loader.loadAsync(url);
          if (cancelled) return null;
          fallbackPoints = new THREE.Points(
            geom,
            new THREE.PointsMaterial({
              size: 0.02,
              vertexColors: geom.hasAttribute("color"),
              color: geom.hasAttribute("color") ? 0xffffff : 0x66ccff,
            }),
          );
          groupRef.current?.add(fallbackPoints);
          return null;
        });
        if (!mod || cancelled) return;

        const { SparkRenderer, SplatMesh } = mod as {
          SparkRenderer: new (opts: { renderer: THREE.WebGLRenderer }) => THREE.Object3D;
          SplatMesh: new (opts: { url: string }) => THREE.Object3D;
        };

        spark = new SparkRenderer({ renderer: gl });
        scene.add(spark);

        splat = new SplatMesh({ url });
        groupRef.current?.add(splat);
      } catch (e) {
        setError((e as Error).message);
      }
    })();

    return () => {
      cancelled = true;
      if (splat) {
        groupRef.current?.remove(splat);
        const maybeDispose = (splat as unknown as { dispose?: () => void }).dispose;
        try { maybeDispose?.(); } catch { /* ignore */ }
      }
      if (spark) {
        scene.remove(spark);
        const maybeDispose = (spark as unknown as { dispose?: () => void }).dispose;
        try { maybeDispose?.(); } catch { /* ignore */ }
      }
      if (fallbackPoints) {
        groupRef.current?.remove(fallbackPoints);
        fallbackPoints.geometry.dispose();
        (fallbackPoints.material as THREE.Material).dispose();
      }
    };
  }, [url, gl, scene]);

  if (error) {
    return (
      <mesh>
        <boxGeometry args={[1, 1, 1]} />
        <meshBasicMaterial color="hotpink" wireframe />
      </mesh>
    );
  }
  return <group ref={groupRef} />;
}
