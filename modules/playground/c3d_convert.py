"""Convert C3D files to OpenSim .trc (marker trajectories) and .mot (ground
reaction forces) files.

Ported from the GRN_gait project's c3d_to_trc.py / c3d_to_mot.py /
main_c3d2sim.py -- those scripts' gap-fill, rigid-cluster reconstruction, and
axis/yaw-correction logic is already tested against real capture data, so the
conversion math here is kept as close to the original as possible rather than
re-derived against Myotion's own (different) C3D reader.
"""

import os

import ezc3d
import numpy as np

MAX_GAP_FILL_FRAMES = 15  # ~0.15s at 100Hz -- bridges brief occlusion blips only.

DEFAULT_RIGID_CLUSTERS = {
    "pelvis": ["LASI", "RASI", "LPSI", "RPSI"],
}


class C3dConvertError(Exception):
    pass


# ---------------------------------------------------------------------------
# TRC (marker trajectories)
# ---------------------------------------------------------------------------

def _legacy_zup_to_yup(marker_points):
    transformed = marker_points[[0, 2, 1], :, :].copy()
    transformed[2, :, :] *= -1
    return transformed


def _yaw_clockwise_90(points_or_vectors):
    rotated = points_or_vectors[[2, 1, 0], :, :].copy()
    rotated[0, :, :] *= -1
    return rotated


def _yaw_correction(points, degrees):
    if degrees == 0:
        return points
    theta = np.radians(degrees)
    cos_t, sin_t = np.cos(theta), np.sin(theta)
    x, y, z = points[0], points[1], points[2]
    return np.stack([x * cos_t + z * sin_t, y, -x * sin_t + z * cos_t], axis=0)


def _kabsch_rotation(source, target):
    m = source @ target.T
    u, _, vt = np.linalg.svd(m)
    v = vt.T
    d = np.sign(np.linalg.det(v @ u.T)) or 1.0
    return v @ np.diag([1.0, 1.0, d]) @ u.T


def _minimal_rotation(a, b):
    a = a / np.linalg.norm(a)
    b = b / np.linalg.norm(b)
    v = np.cross(a, b)
    c = np.dot(a, b)
    s = np.linalg.norm(v)
    if s < 1e-9:
        if c > 0:
            return np.eye(3)
        perp = np.cross(a, [1.0, 0.0, 0.0])
        if np.linalg.norm(perp) < 1e-9:
            perp = np.cross(a, [0.0, 1.0, 0.0])
        perp /= np.linalg.norm(perp)
        return 2 * np.outer(perp, perp) - np.eye(3)
    vx = np.array([[0, -v[2], v[1]], [v[2], 0, -v[0]], [-v[1], v[0], 0]])
    return np.eye(3) + vx + vx @ vx * ((1 - c) / (s ** 2))


def _average_rigid_template(ref_frames):
    centroids = ref_frames.mean(axis=1, keepdims=True)
    centered = ref_frames - centroids
    base = centered[:, :, 0]
    total = base.copy()
    for k in range(1, ref_frames.shape[2]):
        r = _kabsch_rotation(centered[:, :, k], base)
        total += r @ centered[:, :, k]
    return total / ref_frames.shape[2]


def _solve_rigid_and_reconstruct(template, cluster_frame, present_mask):
    n_present = int(present_mask.sum())
    if n_present < 2:
        return None

    obs = cluster_frame[:, present_mask]
    tmpl_obs = template[:, present_mask]

    if n_present >= 3:
        obs_c = obs.mean(axis=1, keepdims=True)
        tmpl_c = tmpl_obs.mean(axis=1, keepdims=True)
        r = _kabsch_rotation(tmpl_obs - tmpl_c, obs - obs_c)
        t = obs_c - r @ tmpl_c
    else:
        tmpl_vec = tmpl_obs[:, 1] - tmpl_obs[:, 0]
        obs_vec = obs[:, 1] - obs[:, 0]
        r = _minimal_rotation(tmpl_vec, obs_vec)
        tmpl_mid = tmpl_obs.mean(axis=1, keepdims=True)
        obs_mid = obs.mean(axis=1, keepdims=True)
        t = obs_mid - r @ tmpl_mid

    return r @ template + t


def _rigid_cluster_fill(marker_points, labels_markers, clusters=DEFAULT_RIGID_CLUSTERS, min_ref_frames=5):
    filled = marker_points.copy()
    name_to_idx = {name: i for i, name in enumerate(labels_markers)}

    for marker_names in clusters.values():
        idxs = [name_to_idx[m] for m in marker_names if m in name_to_idx]
        if len(idxs) < 3:
            continue

        cluster = marker_points[:, idxs, :]
        present = ~np.isnan(cluster).any(axis=0)
        ref_mask = present.all(axis=0)
        if ref_mask.sum() < min_ref_frames:
            continue

        template = _average_rigid_template(cluster[:, :, ref_mask])
        for f in range(marker_points.shape[2]):
            missing = ~present[:, f]
            if not missing.any():
                continue
            reconstructed = _solve_rigid_and_reconstruct(template, cluster[:, :, f], present[:, f])
            if reconstructed is None:
                continue
            for local_i in np.where(missing)[0]:
                filled[:, idxs[local_i], f] = reconstructed[:, local_i]

    return filled


def _fill_short_gaps(marker_points, max_gap):
    filled = marker_points.copy()
    n_axes, n_markers, n_frames = filled.shape
    for m in range(n_markers):
        for a in range(n_axes):
            series = filled[a, m, :]
            isnan = np.isnan(series)
            if not isnan.any():
                continue
            i = 0
            while i < n_frames:
                if isnan[i]:
                    j = i
                    while j < n_frames and isnan[j]:
                        j += 1
                    gap_len = j - i
                    if 0 < i and j < n_frames and gap_len <= max_gap:
                        series[i:j] = np.linspace(series[i - 1], series[j], gap_len + 2)[1:-1]
                    i = j
                else:
                    i += 1
    return filled


def c3d_to_trc(c3d_path, trc_path=None, yaw_correction_deg=0.0):
    """Convert a C3D file's marker (POINT) data to an OpenSim .trc file.

    Only 3D marker trajectories are retrieved -- analog data (force plates,
    EMG) and computed data (angles, powers) are not part of a .trc file.
    Returns the trc_path written.
    """
    if trc_path is None:
        trc_path = os.path.splitext(c3d_path)[0] + ".trc"

    c3d_obj = ezc3d.c3d(c3d_path)

    labels = c3d_obj["parameters"]["POINT"]["LABELS"]["value"]
    if labels is None or len(labels) == 0:
        raise C3dConvertError(
            f"C3D file has no point trajectories: {c3d_path}. "
            "C3D -> TRC requires marker POINT data and cannot convert force-only C3D files."
        )

    points = c3d_obj["data"]["points"]
    if points is None or points.size == 0 or points.shape[1] == 0 or points.shape[2] == 0:
        raise C3dConvertError(
            f"C3D file has no readable point trajectories: {c3d_path}. "
            "C3D -> TRC requires marker POINT data and cannot convert force-only C3D files."
        )

    unit_scale = 1.0
    units_param = c3d_obj["parameters"]["POINT"].get("UNITS", {}).get("value", [])
    units_value = units_param[0] if len(units_param) > 0 else "m"
    if isinstance(units_value, bytes):
        units_value = units_value.decode("utf-8")
    units_value = str(units_value).lower()
    if "mm" in units_value:
        unit_scale = 0.001
    elif "cm" in units_value:
        unit_scale = 0.01
    elif "dm" in units_value:
        unit_scale = 0.1

    index_labels_markers = [
        i for i, s in enumerate(labels)
        if "Angle" not in s and "Power" not in s and "Force" not in s and "Moment" not in s and "GRF" not in s
        and not str(s).strip().startswith("*") and str(s).strip() != ""
    ]
    labels_markers = [labels[ind].strip() for ind in index_labels_markers]

    marker_points = points[:3, index_labels_markers, :] * unit_scale

    valid_marker_mask = np.isfinite(marker_points).any(axis=(0, 2))
    index_labels_markers = [ind for ind, keep in zip(index_labels_markers, valid_marker_mask) if keep]
    labels_markers = [label for label, keep in zip(labels_markers, valid_marker_mask) if keep]
    marker_points = marker_points[:, valid_marker_mask, :]

    if len(labels_markers) == 0:
        raise C3dConvertError(f"C3D file has no valid marker trajectories after filtering: {c3d_path}.")

    frame_rate = float(c3d_obj["header"]["points"]["frame_rate"])
    first_frame = int(c3d_obj["header"]["points"]["first_frame"])
    last_frame = int(c3d_obj["header"]["points"]["last_frame"])
    num_frames = last_frame - first_frame + 1

    header0_str = "PathFileType\t4\t(X/Y/Z)\t" + trc_path
    header1 = {
        "DataRate": str(int(frame_rate)),
        "CameraRate": str(int(frame_rate)),
        "NumFrames": str(num_frames),
        "NumMarkers": str(len(labels_markers)),
        "Units": "m",
        "OrigDataRate": str(int(frame_rate)),
        "OrigDataStartFrame": str(first_frame),
        "OrigNumFrames": str(num_frames),
    }
    header1_str1 = "\t".join(header1.keys())
    header1_str2 = "\t".join(header1.values())
    header2_str1 = "Frame#\tTime\t" + "\t\t\t".join(labels_markers) + "\t\t"
    header2_str2 = "\t\t" + "\t".join(
        "X{i}\tY{i}\tZ{i}".format(i=i + 1) for i in range(int(header1["NumMarkers"]))
    )
    header_trc = "\n".join([header0_str, header1_str1, header1_str2, header2_str1, header2_str2])

    with open(trc_path, "w") as trc_o:
        trc_o.write(header_trc + "\n")

        marker_points = np.where(np.isfinite(marker_points), marker_points, np.nan)
        marker_points = _rigid_cluster_fill(marker_points, labels_markers)
        marker_points = _fill_short_gaps(marker_points, MAX_GAP_FILL_FRAMES)
        marker_points = _legacy_zup_to_yup(marker_points)
        marker_points = _yaw_clockwise_90(marker_points)
        marker_points = _yaw_correction(marker_points, yaw_correction_deg)
        trc_time = np.linspace(first_frame / frame_rate, last_frame / frame_rate, num=num_frames)
        frame_numbers = np.arange(first_frame, last_frame + 1)

        for n in range(num_frames):
            c3d_line_markers = marker_points[:, :, n].T.flatten()
            trc_line = "{i}\t{t}\t".format(i=frame_numbers[n], t=trc_time[n]) + "\t".join(
                map(str, c3d_line_markers)
            )
            trc_o.write(trc_line + "\n")

    return trc_path


# ---------------------------------------------------------------------------
# MOT (ground reaction forces)
# ---------------------------------------------------------------------------

def _zup_to_yup(arr):
    out = np.zeros_like(arr)
    out[0, :] = arr[0, :]
    out[1, :] = arr[2, :]
    out[2, :] = -arr[1, :]
    return out


def _mot_yaw_clockwise_90(arr):
    out = np.zeros_like(arr)
    out[0, :] = -arr[2, :]
    out[1, :] = arr[1, :]
    out[2, :] = arr[0, :]
    return out


def _mot_yaw_correction(arr, degrees):
    if degrees == 0:
        return arr
    theta = np.radians(degrees)
    cos_t, sin_t = np.cos(theta), np.sin(theta)
    out = np.zeros_like(arr)
    x, y, z = arr[0, :], arr[1, :], arr[2, :]
    out[0, :] = x * cos_t + z * sin_t
    out[1, :] = y
    out[2, :] = -x * sin_t + z * cos_t
    return out


def _downsample(arr, from_rate, to_rate):
    ratio = from_rate / to_rate
    if abs(ratio - round(ratio)) > 1e-6:
        raise C3dConvertError(f"Output rate {to_rate} Hz is not an integer divisor of analog rate {from_rate} Hz.")
    step = int(round(ratio))
    return arr[:, ::step]


def _cop_from_force_moment(force_n_zup, moment_nm_zup, origin_mm, force_threshold):
    n = force_n_zup.shape[1]
    fz = force_n_zup[2, :]
    contact = np.abs(fz) > force_threshold
    safe_fz = np.where(contact, fz, 1.0)

    cop_mm = np.zeros((3, n))
    cop_mm[0, :] = -moment_nm_zup[1, :] * 1000.0 / safe_fz + origin_mm[0]
    cop_mm[1, :] = moment_nm_zup[0, :] * 1000.0 / safe_fz + origin_mm[1]
    cop_mm[2, :] = origin_mm[2]
    cop_mm[:, ~contact] = 0.0
    return cop_mm


def _source_is_valid(cop_mm, contact, cop_min_std_mm):
    if cop_mm is None or np.count_nonzero(contact) <= 1:
        return False, 0.0
    std_val = float(np.max(np.std(cop_mm[:, contact], axis=1)))
    return std_val > cop_min_std_mm, std_val


def _load_forceplate_data(c3d_path, threshold=20.0, cop_min_std_mm=1.0, moment_unit_scale=0.001,
                           yaw_correction_deg=0.0):
    c = ezc3d.c3d(c3d_path, extract_forceplat_data=True)

    analog_rate = float(c["parameters"]["ANALOG"]["RATE"]["value"][0])
    marker_rate = float(c["header"]["points"]["frame_rate"])
    first_frame = int(c["header"]["points"]["first_frame"])
    last_frame = int(c["header"]["points"]["last_frame"])
    n_point_frames = last_frame - first_frame + 1

    platform_data = c["data"]["platform"]
    if len(platform_data) == 0:
        raise C3dConvertError(f"No force-plate data found in '{c3d_path}'.")

    n_analog = platform_data[0]["force"].shape[1]
    expected_analog = n_point_frames * int(round(analog_rate / marker_rate))
    if n_analog > expected_analog:
        n_analog = expected_analog

    plates = []
    for i in range(len(platform_data)):
        force_raw = platform_data[i]["force"][:, :n_analog]
        moment_raw = platform_data[i]["moment"][:, :n_analog] * moment_unit_scale
        cop_platform_mm = np.nan_to_num(platform_data[i]["center_of_pressure"][:, :n_analog], nan=0.0)

        contact = np.abs(force_raw[2, :]) >= threshold
        origin_raw = platform_data[i].get("origin", np.zeros(3))
        origin_mm = np.asarray(origin_raw).reshape(-1)[:3]

        platform_ok, platform_std = _source_is_valid(cop_platform_mm, contact, cop_min_std_mm)
        cop_recomputed_mm = _cop_from_force_moment(force_raw, moment_raw, origin_mm, threshold)
        recomputed_ok, recomputed_std = _source_is_valid(cop_recomputed_mm, contact, cop_min_std_mm)

        cop_mm = cop_platform_mm.copy() if platform_ok else cop_recomputed_mm.copy()

        force_masked = force_raw.copy()
        moment_masked = moment_raw.copy()
        cop_masked = cop_mm.copy()
        force_masked[:, ~contact] = 0.0
        moment_masked[:, ~contact] = 0.0
        cop_masked[:, ~contact] = 0.0

        force_os = _mot_yaw_correction(_mot_yaw_clockwise_90(_zup_to_yup(force_masked)), yaw_correction_deg)
        moment_os = _mot_yaw_correction(_mot_yaw_clockwise_90(_zup_to_yup(moment_masked)), yaw_correction_deg)
        cop_os = _mot_yaw_correction(_mot_yaw_clockwise_90(_zup_to_yup(cop_masked)), yaw_correction_deg) / 1000.0

        torque_os = moment_os.copy()
        torque_os[:, ~contact] = 0.0

        plates.append({"force": force_os, "cop": cop_os, "torque": torque_os})

    return plates, analog_rate, marker_rate, n_point_frames


def _write_opensim_mot(mot_path, plates, analog_rate, target_rate=None):
    out_rate = target_rate if target_rate is not None else analog_rate

    force_list, cop_list, torque_list = [], [], []
    for plate in plates:
        force_os, cop_os, torque_os = plate["force"], plate["cop"], plate["torque"]
        if out_rate != analog_rate:
            force_os = _downsample(force_os, analog_rate, out_rate)
            cop_os = _downsample(cop_os, analog_rate, out_rate)
            torque_os = _downsample(torque_os, analog_rate, out_rate)
        force_list.append(force_os)
        cop_list.append(cop_os)
        torque_list.append(torque_os)

    n_out = force_list[0].shape[1]
    time = np.arange(n_out) / out_rate
    num_fp = len(plates)
    n_cols = 1 + num_fp * 9

    with open(mot_path, "w", encoding="utf-8") as f:
        f.write(f"{os.path.basename(mot_path)}\n")
        f.write("version = 1\n")
        f.write(f"nRows= {n_out}\n")
        f.write(f"nColumns= {n_cols}\n")
        f.write("inDegrees=yes\n")
        f.write("endheader\n")

        labels = ["time"]
        for i in range(num_fp):
            force_prefix = "ground_force" if i == 0 else f"{i}_ground_force"
            torque_prefix = "ground_torque" if i == 0 else f"{i}_ground_torque"
            labels += [
                f"{force_prefix}_vx", f"{force_prefix}_vy", f"{force_prefix}_vz",
                f"{force_prefix}_px", f"{force_prefix}_py", f"{force_prefix}_pz",
                f"{torque_prefix}_x", f"{torque_prefix}_y", f"{torque_prefix}_z",
            ]
        f.write("\t".join(labels) + "\n")

        for k in range(n_out):
            row = [f"{time[k]:.6f}"]
            for i in range(num_fp):
                fx, fy, fz = force_list[i][:, k]
                px, py, pz = cop_list[i][:, k]
                tx, ty, tz = torque_list[i][:, k]
                row += [f"{fx:.6f}", f"{fy:.6f}", f"{fz:.6f}",
                        f"{px:.6f}", f"{py:.6f}", f"{pz:.6f}",
                        f"{tx:.6f}", f"{ty:.6f}", f"{tz:.6f}"]
            f.write("\t".join(row) + "\n")


def c3d_to_mot(c3d_path, mot_path=None, target_rate=None, threshold=20.0,
               cop_min_std_mm=1.0, moment_unit_scale=0.001, yaw_correction_deg=0.0):
    """Convert a C3D file's force-plate data to an OpenSim external-loads .mot file.

    Raises C3dConvertError if the file has no force-plate data. Returns the
    mot_path written.
    """
    if mot_path is None:
        mot_path = os.path.splitext(c3d_path)[0] + "_grf.mot"

    plates, analog_rate, marker_rate, n_point_frames = _load_forceplate_data(
        c3d_path=c3d_path, threshold=threshold, cop_min_std_mm=cop_min_std_mm,
        moment_unit_scale=moment_unit_scale, yaw_correction_deg=yaw_correction_deg,
    )
    _write_opensim_mot(mot_path, plates, analog_rate, target_rate=target_rate)
    return mot_path


# ---------------------------------------------------------------------------
# Batch driver
# ---------------------------------------------------------------------------

def find_c3d_files(input_folder, recursive=False):
    pattern = "**/*.c3d" if recursive else "*.c3d"
    from pathlib import Path
    return sorted(p for p in Path(input_folder).glob(pattern) if p.is_file())


def convert_c3d_batch(c3d_files, output_folder, yaw_correction_deg=0.0,
                       mot_threshold=20.0, mot_rate=None, progress_cb=None):
    """Convert a list of C3D paths to .trc (always attempted) and .mot (only
    if the file has force-plate data) files under output_folder.

    progress_cb, if given, is called after each file as
    progress_cb(index, total, c3d_path, trc_result, mot_result) where each
    result is (True, out_path) or (False, error_message).
    """
    os.makedirs(output_folder, exist_ok=True)
    total = len(c3d_files)
    results = []

    for idx, c3d_path in enumerate(c3d_files, start=1):
        c3d_path = str(c3d_path)
        stem = os.path.splitext(os.path.basename(c3d_path))[0]
        trc_out = os.path.join(output_folder, stem + ".trc")
        mot_out = os.path.join(output_folder, stem + "_grf.mot")

        try:
            c3d_to_trc(c3d_path, trc_out, yaw_correction_deg=yaw_correction_deg)
            trc_result = (True, trc_out)
        except Exception as exc:
            trc_result = (False, str(exc))

        try:
            c3d_to_mot(c3d_path, mot_out, target_rate=mot_rate, threshold=mot_threshold,
                       yaw_correction_deg=yaw_correction_deg)
            mot_result = (True, mot_out)
        except Exception as exc:
            mot_result = (False, str(exc))

        results.append((c3d_path, trc_result, mot_result))
        if progress_cb is not None:
            progress_cb(idx, total, c3d_path, trc_result, mot_result)

    return results
