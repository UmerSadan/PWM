"""
Synthetic multi-view stereo pipeline: build a ground-truth object, simulate an
orbiting camera rig filming it, triangulate a point cloud from the noisy 2D
observations, refine it, and export the result.

Pipeline stages:
    1. BUILD        - construct a ground-truth object (a small stylized tower)
    2. CAPTURE       - simulate an orbiting camera rig observing the object
    3. RECONSTRUCT   - linear (DLT) multi-view triangulation
    4. REFINE        - nonlinear reprojection-error refinement per point   [NEW]
    5. FILTER        - drop points whose reprojection error is too high    [NEW]
    6. EXPORT        - write .ply point cloud + JSON stats + diagnostic plots

See README.md for a full list of what was changed/added and why.
"""

import argparse
import json
import numpy as np
from scipy.optimize import least_squares

# ---------------------------------------------------------------------------
# CLI  [NEW] - the pipeline used to be hardcoded; now key parameters (frame
# count, pixel noise, output paths, outlier threshold) are configurable
# without touching the source.
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(description="Synthetic multi-view reconstruction pipeline")
    p.add_argument("--frames", type=int, default=14, help="Number of orbit camera frames")
    p.add_argument("--pixel-noise-std", type=float, default=1.1, help="Std dev of pixel noise (px)")
    p.add_argument("--reproj-threshold", type=float, default=2.5,
                   help="Max mean reprojection error (px) allowed to keep a point")
    p.add_argument("--seed", type=int, default=11, help="RNG seed")
    p.add_argument("--out-dir", type=str, default=".", help="Directory to write outputs to")
    p.add_argument("--no-refine", action="store_true",
                   help="Skip nonlinear refinement (use raw DLT triangulation only)")
    return p.parse_args()


args = parse_args()
rng = np.random.default_rng(args.seed)

# ---------------------------------------------------------------------------
# 1. BUILD: construct the ground-truth object (a small stylized tower)
# ---------------------------------------------------------------------------

def sample_box_surface(center, size, n_points, color):
    """Sample points on the 6 faces of an axis-aligned box, with outward normals."""
    cx, cy, cz = center
    sx, sy, sz = size
    pts, normals, colors = [], [], []
    faces = [
        (np.array([1, 0, 0]), sx / 2), (np.array([-1, 0, 0]), sx / 2),
        (np.array([0, 1, 0]), sy / 2), (np.array([0, -1, 0]), sy / 2),
        (np.array([0, 0, 1]), sz / 2), (np.array([0, 0, -1]), sz / 2),
    ]
    per_face = max(n_points // 6, 1)
    for normal, offset in faces:
        for _ in range(per_face):
            if normal[0] != 0:
                p = np.array([cx + normal[0] * offset, cy + rng.uniform(-sy / 2, sy / 2), cz + rng.uniform(-sz / 2, sz / 2)])
            elif normal[1] != 0:
                p = np.array([cx + rng.uniform(-sx / 2, sx / 2), cy + normal[1] * offset, cz + rng.uniform(-sz / 2, sz / 2)])
            else:
                p = np.array([cx + rng.uniform(-sx / 2, sx / 2), cy + rng.uniform(-sy / 2, sy / 2), cz + normal[2] * offset])
            pts.append(p)
            normals.append(normal)
            colors.append(color)
    return pts, normals, colors


def sample_pyramid_surface(apex, base_center, base_size, n_points, color):
    """Sample points on the 4 triangular faces of a square pyramid (roof)."""
    pts, normals, colors = [], [], []
    bx, by, bz = base_center
    s = base_size / 2
    corners = [
        np.array([bx - s, by - s, bz]), np.array([bx + s, by - s, bz]),
        np.array([bx + s, by + s, bz]), np.array([bx - s, by + s, bz]),
    ]
    for i in range(4):
        c0, c1 = corners[i], corners[(i + 1) % 4]
        edge1 = c1 - c0
        edge2 = apex - c0
        normal = np.cross(edge1, edge2)
        normal = normal / (np.linalg.norm(normal) + 1e-9)
        if normal[2] < 0:
            normal = -normal
        for _ in range(n_points // 4):
            a = rng.uniform(0, 1)
            b = rng.uniform(0, 1 - a)
            p = c0 + a * edge1 + b * edge2
            pts.append(p)
            normals.append(normal)
            colors.append(color)
    return pts, normals, colors


all_pts, all_normals, all_colors = [], [], []

# Base plinth
p, n, c = sample_box_surface((0, 0, 0.3), (2.4, 2.4, 0.6), 260, (120, 120, 120))
all_pts += p; all_normals += n; all_colors += c

# Tower shaft
p, n, c = sample_box_surface((0, 0, 2.0), (1.4, 1.4, 2.6), 420, (200, 190, 165))
all_pts += p; all_normals += n; all_colors += c

# Upper shaft (narrower)
p, n, c = sample_box_surface((0, 0, 3.9), (1.0, 1.0, 1.0), 260, (180, 170, 150))
all_pts += p; all_normals += n; all_colors += c

# Pyramid roof
p, n, c = sample_pyramid_surface(
    apex=np.array([0, 0, 5.4]), base_center=(0, 0, 4.4), base_size=1.1,
    n_points=260, color=(178, 58, 46),
)
all_pts += p; all_normals += n; all_colors += c

# NEW: a small flag/spire on top of the roof so the silhouette has a fine
# high-frequency feature to stress-test triangulation accuracy.
flag_pts, flag_normals, flag_colors = [], [], []
pole_top = np.array([0, 0, 6.1])
pole_base = np.array([0, 0, 5.4])
for t in np.linspace(0, 1, 40):
    center = pole_base + t * (pole_top - pole_base)
    for _ in range(4):
        ang = rng.uniform(0, 2 * np.pi)
        r = 0.03
        offset = np.array([r * np.cos(ang), r * np.sin(ang), 0])
        flag_pts.append(center + offset)
        n = offset / (np.linalg.norm(offset) + 1e-9)
        flag_normals.append(n)
        flag_colors.append((60, 60, 65))
all_pts += flag_pts; all_normals += flag_normals; all_colors += flag_colors

ground_truth = np.array(all_pts)
normals = np.array(all_normals)
colors = np.array(all_colors, dtype=np.uint8)
N = len(ground_truth)
print(f"Build model: {N} ground-truth surface points")

# ---------------------------------------------------------------------------
# 2. CAPTURE: simulate an orbiting camera rig filming the build
# ---------------------------------------------------------------------------

IMG_W, IMG_H = 960, 720
FOCAL = 900.0
CX, CY = IMG_W / 2, IMG_H / 2
K = np.array([[FOCAL, 0, CX], [0, FOCAL, CY], [0, 0, 1]])

target = np.array([0, 0, 2.5])
N_FRAMES = args.frames
ORBIT_RADIUS = 9.0
cameras = []  # each: dict(R, t, C)

for i in range(N_FRAMES):
    theta = 2 * np.pi * i / N_FRAMES
    height = 2.5 + 1.8 * np.sin(theta * 1.3)
    cam_pos = np.array([
        ORBIT_RADIUS * np.cos(theta),
        ORBIT_RADIUS * np.sin(theta),
        height,
    ])
    forward = target - cam_pos
    forward /= np.linalg.norm(forward)
    world_up = np.array([0, 0, 1])
    right = np.cross(forward, world_up)
    right /= np.linalg.norm(right)
    true_up = np.cross(right, forward)
    # camera looks along +z_cam = forward; x_cam = right; y_cam = -true_up (image y down)
    R = np.stack([right, -true_up, forward], axis=0)  # world -> camera rotation
    t = -R @ cam_pos
    cameras.append({"R": R, "t": t, "C": cam_pos})

print(f"Simulated {N_FRAMES} camera frames orbiting the build")

# Project every ground-truth point into every frame; keep noisy 2D pixel
# observations only where the point faces the camera (visibility) and lands
# inside the image bounds (this mimics feature detection on real footage).
PIXEL_NOISE_STD = args.pixel_noise_std
observations = [[] for _ in range(N)]  # observations[i] = list of (frame_idx, u, v)

for f_idx, cam in enumerate(cameras):
    R, t, C = cam["R"], cam["t"], cam["C"]
    for i in range(N):
        Xw = ground_truth[i]
        view_dir = C - Xw
        if np.dot(normals[i], view_dir) <= 0.05:
            continue  # back-facing, not visible in this frame
        Xc = R @ Xw + t
        if Xc[2] <= 0.1:
            continue
        u = FOCAL * Xc[0] / Xc[2] + CX
        v = FOCAL * Xc[1] / Xc[2] + CY
        if not (0 <= u < IMG_W and 0 <= v < IMG_H):
            continue
        u_noisy = u + rng.normal(0, PIXEL_NOISE_STD)
        v_noisy = v + rng.normal(0, PIXEL_NOISE_STD)
        observations[i].append((f_idx, u_noisy, v_noisy))

n_with_2plus = sum(1 for obs in observations if len(obs) >= 2)
print(f"{n_with_2plus}/{N} points observed in >=2 frames (triangulable)")

# ---------------------------------------------------------------------------
# 3. RECONSTRUCT: linear (DLT) multi-view triangulation
# ---------------------------------------------------------------------------

def triangulate_point(obs, cameras, K):
    """Linear least-squares triangulation from >=2 noisy pixel observations."""
    A = []
    for f_idx, u, v in obs:
        R, t = cameras[f_idx]["R"], cameras[f_idx]["t"]
        P = K @ np.hstack([R, t.reshape(3, 1)])  # 3x4 projection matrix
        A.append(u * P[2] - P[0])
        A.append(v * P[2] - P[1])
    A = np.array(A)
    _, _, Vt = np.linalg.svd(A)
    Xh = Vt[-1]
    return Xh[:3] / Xh[3]


# ---------------------------------------------------------------------------
# 4. REFINE  [NEW]: nonlinear reprojection-error minimization.
#
# Linear DLT triangulation minimizes an *algebraic* error, which is not the
# same as minimizing actual pixel reprojection error, especially with >2
# views and noisy correspondences. This stage runs a small per-point
# Levenberg-Marquardt refinement (via scipy.optimize.least_squares) that
# starts from the DLT solution and adjusts the 3D point to minimize the true
# sum of squared pixel residuals across all observing views. This is a
# minimal, per-point stand-in for full bundle adjustment.
# ---------------------------------------------------------------------------

def reprojection_residuals(Xw, obs, cameras, K):
    res = []
    for f_idx, u, v in obs:
        R, t = cameras[f_idx]["R"], cameras[f_idx]["t"]
        Xc = R @ Xw + t
        if Xc[2] <= 1e-6:
            res += [1e3, 1e3]  # heavy penalty for points behind camera
            continue
        u_hat = FOCAL * Xc[0] / Xc[2] + CX
        v_hat = FOCAL * Xc[1] / Xc[2] + CY
        res += [u_hat - u, v_hat - v]
    return np.array(res)


def refine_point(X0, obs, cameras, K):
    result = least_squares(
        reprojection_residuals, X0, args=(obs, cameras, K),
        method="lm", max_nfev=200,
    )
    return result.x


def mean_reprojection_error(Xw, obs, cameras, K):
    res = reprojection_residuals(Xw, obs, cameras, K)
    res = res.reshape(-1, 2)
    return np.linalg.norm(res, axis=1).mean()


reconstructed = []
recon_colors = []
gt_errors = []          # distance to synthetic ground truth (only possible since we simulated it)
reproj_errors = []      # pixel reprojection error (the metric available in real-world use)
n_obs_kept = []

for i in range(N):
    obs = observations[i]
    if len(obs) < 2:
        continue
    X_dlt = triangulate_point(obs, cameras, K)
    if args.no_refine:
        X_final = X_dlt
    else:
        X_final = refine_point(X_dlt, obs, cameras, K)

    reconstructed.append(X_final)
    recon_colors.append(colors[i])
    gt_errors.append(np.linalg.norm(X_final - ground_truth[i]))
    reproj_errors.append(mean_reprojection_error(X_final, obs, cameras, K))
    n_obs_kept.append(len(obs))

reconstructed = np.array(reconstructed)
recon_colors = np.array(recon_colors, dtype=np.uint8)
gt_errors = np.array(gt_errors)
reproj_errors = np.array(reproj_errors)
n_obs_kept = np.array(n_obs_kept)

print(f"Triangulated {len(reconstructed)} 3D points")
print(f"[pre-refine vs post-refine would differ per-point; reporting final values]")
print(f"Mean 3D error vs ground truth: {gt_errors.mean():.4f} world units "
      f"(median {np.median(gt_errors):.4f}, max {gt_errors.max():.4f})")
print(f"Mean reprojection error: {reproj_errors.mean():.4f} px "
      f"(median {np.median(reproj_errors):.4f}, max {reproj_errors.max():.4f})")

# ---------------------------------------------------------------------------
# 5. FILTER  [NEW]: drop points with high reprojection error.
#
# In a real pipeline there is no ground truth to check against, so quality
# control has to rely on reprojection error - exactly what real SfM/MVS
# tools (COLMAP, etc.) use to prune bad triangulations caused by outlier
# correspondences or degenerate camera geometry (near-parallel rays).
# ---------------------------------------------------------------------------

keep_mask = reproj_errors <= args.reproj_threshold
n_dropped = int((~keep_mask).sum())

final_points = reconstructed[keep_mask]
final_colors = recon_colors[keep_mask]
final_gt_errors = gt_errors[keep_mask]
final_reproj_errors = reproj_errors[keep_mask]

print(f"Outlier filter (reproj error <= {args.reproj_threshold} px): "
      f"kept {len(final_points)}, dropped {n_dropped}")

# ---------------------------------------------------------------------------
# 6. EXPORT: write .ply point cloud, JSON stats, and diagnostic plots  [NEW: stats + plots]
# ---------------------------------------------------------------------------

def write_ply(path, points, colors):
    with open(path, "w") as f:
        f.write("ply\n")
        f.write("format ascii 1.0\n")
        f.write(f"element vertex {len(points)}\n")
        f.write("property float x\n")
        f.write("property float y\n")
        f.write("property float z\n")
        f.write("property uchar red\n")
        f.write("property uchar green\n")
        f.write("property uchar blue\n")
        f.write("end_header\n")
        for p, c in zip(points, colors):
            f.write(f"{p[0]:.6f} {p[1]:.6f} {p[2]:.6f} {int(c[0])} {int(c[1])} {int(c[2])}\n")


import os
os.makedirs(args.out_dir, exist_ok=True)

ply_path = os.path.join(args.out_dir, "reconstruction.ply")
write_ply(ply_path, final_points, final_colors)
print(f"Saved {ply_path}")

stats = {
    "config": {
        "n_frames": N_FRAMES,
        "pixel_noise_std": PIXEL_NOISE_STD,
        "reproj_threshold_px": args.reproj_threshold,
        "refinement_enabled": not args.no_refine,
        "seed": args.seed,
    },
    "counts": {
        "ground_truth_points": int(N),
        "points_with_2plus_views": int(n_with_2plus),
        "points_triangulated": int(len(reconstructed)),
        "points_kept_after_filter": int(len(final_points)),
        "points_dropped_as_outliers": n_dropped,
    },
    "accuracy_vs_ground_truth_world_units": {
        "mean": float(final_gt_errors.mean()) if len(final_gt_errors) else None,
        "median": float(np.median(final_gt_errors)) if len(final_gt_errors) else None,
        "max": float(final_gt_errors.max()) if len(final_gt_errors) else None,
    },
    "reprojection_error_px": {
        "mean": float(final_reproj_errors.mean()) if len(final_reproj_errors) else None,
        "median": float(np.median(final_reproj_errors)) if len(final_reproj_errors) else None,
        "max": float(final_reproj_errors.max()) if len(final_reproj_errors) else None,
    },
}
stats_path = os.path.join(args.out_dir, "reconstruction_stats.json")
with open(stats_path, "w") as f:
    json.dump(stats, f, indent=2)
print(f"Saved {stats_path}")

# Diagnostic plots
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d import Axes3D  # noqa: F401

fig, axes = plt.subplots(1, 2, figsize=(11, 4.5))
axes[0].hist(reproj_errors, bins=30, color="#3b6ea5", edgecolor="white")
axes[0].axvline(args.reproj_threshold, color="crimson", linestyle="--",
                 label=f"filter threshold = {args.reproj_threshold}px")
axes[0].set_xlabel("Mean reprojection error (px)")
axes[0].set_ylabel("Point count")
axes[0].set_title("Reprojection error distribution")
axes[0].legend()

axes[1].hist(gt_errors, bins=30, color="#3ba55d", edgecolor="white")
axes[1].set_xlabel("3D error vs ground truth (world units)")
axes[1].set_ylabel("Point count")
axes[1].set_title("Triangulation accuracy (sim-only check)")
plt.tight_layout()
hist_path = os.path.join(args.out_dir, "error_histograms.png")
plt.savefig(hist_path, dpi=140)
plt.close(fig)
print(f"Saved {hist_path}")

fig = plt.figure(figsize=(6, 6))
ax = fig.add_subplot(111, projection="3d")
cam_positions = np.array([c["C"] for c in cameras])
ax.plot(cam_positions[:, 0], cam_positions[:, 1], cam_positions[:, 2],
        "o-", color="#e07b39", label="camera path")
sample_idx = rng.choice(len(final_points), size=min(1500, len(final_points)), replace=False)
ax.scatter(final_points[sample_idx, 0], final_points[sample_idx, 1], final_points[sample_idx, 2],
           c=final_colors[sample_idx] / 255.0, s=3)
ax.set_title("Camera orbit + reconstructed point cloud")
ax.legend()
scene_path = os.path.join(args.out_dir, "camera_and_cloud.png")
plt.savefig(scene_path, dpi=140)
plt.close(fig)
print(f"Saved {scene_path}")
