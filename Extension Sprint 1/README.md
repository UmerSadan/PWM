# Synthetic Multi-View Reconstruction Pipeline

This script builds a small synthetic 3D object (a stylized tower), simulates
an orbiting camera rig filming it, triangulates a point cloud from the noisy
2D observations, and exports the result as a `.ply` file.

Run it with:

```bash
python3 reconstruction.py --out-dir ./out
```

Useful flags:

| Flag | Default | What it does |
|---|---|---|
| `--frames` | 14 | Number of orbit camera frames |
| `--pixel-noise-std` | 1.1 | Simulated 2D feature-detection noise (px) |
| `--reproj-threshold` | 2.5 | Max mean reprojection error (px) a point may have to be kept |
| `--no-refine` | off | Skip the nonlinear refinement stage (raw DLT only) |
| `--seed` | 11 | RNG seed, for reproducibility |
| `--out-dir` | `.` | Where to write all output files |

## What was added / improved

The original script only did BUILD → CAPTURE → RECONSTRUCT (linear DLT
triangulation) → export a `.ply`. It also had a bit of dead/confusing code in
`sample_box_surface` (unused `u`/`v` variables computed and then discarded).
Here's what changed:

1. **New refinement stage (nonlinear bundle-adjustment-lite).**
   Linear DLT triangulation minimizes an *algebraic* error, not the actual
   pixel reprojection error. A new stage runs a per-point
   Levenberg–Marquardt refinement (`scipy.optimize.least_squares`) that
   starts from the DLT solution and adjusts each 3D point to minimize the
   true sum-of-squared pixel residuals across every view that observed it.
   This is a lightweight, per-point stand-in for full bundle adjustment. It
   can be disabled with `--no-refine` to compare against the raw DLT output.

2. **Reprojection-error-based outlier filtering.**
   In a real capture there's no ground truth to check reconstructed points
   against — the only available quality signal is reprojection error (how
   far a triangulated point's projection lands from the original 2D
   detections). The pipeline now computes this for every point and drops
   any point above `--reproj-threshold` pixels before export, the same
   basic quality-control idea used by real SfM/MVS tools (e.g. COLMAP) to
   prune bad triangulations caused by mismatched correspondences or
   near-degenerate camera geometry (rays that are nearly parallel).

3. **Removed the dead code** in `sample_box_surface` — it computed local `u`,
   `v` offset variables from a confusing conditional expression and then
   never used them; the actual sampling logic used separate `rng.uniform`
   calls a few lines below. Removed the unused computation.

4. **A new object feature: a thin spire/flag pole** on top of the roof
   (`flag_pts`), so the object silhouette has a fine, high-frequency
   detail. Thin structures are exactly where triangulation noise and
   viewing-angle sensitivity show up most, so this is a more meaningful
   stress test than the original all-boxes-and-one-pyramid shape.

5. **Quantitative + visual diagnostics** (previously the script only printed
   a couple of summary lines to stdout):
   - `reconstruction_stats.json` — structured run report: config used, point
     counts at every pipeline stage, mean/median/max 3D error vs ground
     truth, and mean/median/max reprojection error.
   - `error_histograms.png` — side-by-side histograms of reprojection error
     (with the filter threshold marked) and 3D ground-truth error.
   - `camera_and_cloud.png` — 3D plot of the camera orbit path together with
     the reconstructed, colored point cloud, useful for a quick visual
     sanity check of coverage and camera geometry.

6. **Command-line configurability.** Frame count, pixel noise, the outlier
   threshold, the RNG seed, whether refinement runs, and the output
   directory are now all CLI flags (`argparse`) instead of hardcoded
   constants, so the same script can be used to explore how these factors
   affect reconstruction quality without editing the source.

## Output files

Running the script produces, in `--out-dir`:

- `reconstruction.ply` — the final, filtered, colored point cloud (ASCII PLY)
- `reconstruction_stats.json` — run configuration + accuracy/quality metrics
- `error_histograms.png` — reprojection error and ground-truth error distributions
- `camera_and_cloud.png` — camera orbit path + reconstructed cloud, in 3D

## Example results

Default settings (14 frames, 1.1 px noise): ~1194 of 1356 surface points
were visible in 2+ views and triangulated; mean reprojection error ≈1.2 px,
mean 3D error vs. the known ground truth ≈0.009 world units — all points
passed the 2.5 px filter.

Stress test (6 frames, 4.0 px noise): mean reprojection error rises to
~3.0 px and the filter now removes roughly 60% of points as unreliable —
demonstrating the filter actually matters once the capture geometry/noise
gets worse, which is the realistic regime the filtering step is meant for.
Refinement's benefit over raw DLT is small under both settings here (DLT+SVD
is already close to optimal for this well-conditioned multi-view setup), but
it is consistently non-negative and would matter more with fewer views per
point, larger noise, or less well-distributed camera baselines.
