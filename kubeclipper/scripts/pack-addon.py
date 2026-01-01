#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import argparse
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Set, Tuple

try:
    import yaml  # pip install pyyaml
except Exception:
    yaml = None


def run(cmd: List[str], capture: bool = False) -> str:
    if capture:
        out = subprocess.check_output(cmd, stderr=subprocess.STDOUT)
        return out.decode(errors="replace")
    subprocess.check_call(cmd, shell=False)
    return ""


def tar_dir(src_dir: str, dst_tar: str) -> None:
    with tarfile.open(dst_tar, "w:gz") as tar:
        tar.add(src_dir, arcname=os.path.basename(src_dir))


def helm_template(chart_path: str, values_path: Optional[str], helm_args: List[str]) -> str:
    cmd = ["helm", "template", chart_path, "--include-crds"]
    if values_path:
        cmd.extend(["-f", values_path])
    if helm_args:
        cmd.extend(helm_args)
    return run(cmd, capture=True)


def normalize_image_from_dict(d: Dict[str, Any]) -> Optional[str]:
    repo = d.get("repository") or d.get("repo") or d.get("name")
    tag = d.get("tag")
    digest = d.get("digest")

    if not repo:
        return None
    if digest:
        return f"{repo}@{digest}" if "@" not in str(repo) else str(repo)
    if tag:
        return f"{repo}:{tag}" if ":" not in str(repo) else str(repo)
    return str(repo)


def collect_images_from_obj(obj: Any, out: List[str], seen: Set[str]) -> None:
    if obj is None:
        return

    if isinstance(obj, dict):
        for k in ("containers", "initContainers", "ephemeralContainers"):
            v = obj.get(k)
            if isinstance(v, list):
                for c in v:
                    if isinstance(c, dict) and isinstance(c.get("image"), str):
                        img = c["image"].strip().strip('"').strip("'")
                        if img and img not in seen:
                            out.append(img)
                            seen.add(img)

        if "image" in obj:
            v = obj.get("image")
            if isinstance(v, str):
                img = v.strip().strip('"').strip("'")
                if img and img not in seen:
                    out.append(img)
                    seen.add(img)
            elif isinstance(v, dict):
                img = normalize_image_from_dict(v)
                if img and img not in seen:
                    out.append(img)
                    seen.add(img)

        for _, v in obj.items():
            collect_images_from_obj(v, out, seen)
        return

    if isinstance(obj, list):
        for it in obj:
            collect_images_from_obj(it, out, seen)
        return


def render_images(chart_path: str, values_path: Optional[str], helm_args: List[str]) -> List[str]:
    text = helm_template(chart_path, values_path, helm_args)

    if yaml is not None:
        images: List[str] = []
        seen: Set[str] = set()
        for doc in yaml.safe_load_all(text):
            collect_images_from_obj(doc, images, seen)
        return images

    print("[WARN] PyYAML not installed, fallback to regex parsing. Recommend: pip install pyyaml")
    images, seen = [], set()
    for line in text.splitlines():
        m = re.search(r"(?:^-?\s*image:\s*)([^\s#]+)", line.strip(), re.IGNORECASE)
        if m:
            img = m.group(1).strip().strip('"').strip("'")
            if img and img not in seen:
                images.append(img)
                seen.add(img)
    return images


def image_exists(tool: str, image: str, ctr_namespace: str) -> bool:
    try:
        if tool == "nerdctl":
            run(["nerdctl", "image", "inspect", image], capture=True)
            return True
        if tool == "docker":
            run(["docker", "image", "inspect", image], capture=True)
            return True
        if tool == "ctr":
            out = run(["ctr", "-n", ctr_namespace, "images", "ls", "-q"], capture=True)
            return any(image == line.strip() or image in line.strip() for line in out.splitlines())
    except subprocess.CalledProcessError:
        return False
    return False


def pull_one(tool: str, image: str, ctr_namespace: str) -> None:
    if tool == "nerdctl":
        run(["nerdctl", "pull", image])
    elif tool == "docker":
        run(["docker", "pull", image])
    elif tool == "ctr":
        run(["ctr", "-n", ctr_namespace, "images", "pull", image])
    else:
        raise RuntimeError(f"unsupported image tool: {tool}")


def pull_images(images: List[str], tool: str, parallel: int, ctr_namespace: str,
                ignore_errors: bool) -> None:
    if not images:
        return

    to_pull = [img for img in images if not image_exists(tool, img, ctr_namespace)]
    if not to_pull:
        print("all images already exist locally; skip pulling.")
        return

    print(f"pulling {len(to_pull)} images using {tool} (parallel={parallel}) ...")
    failures: List[Tuple[str, str]] = []

    with ThreadPoolExecutor(max_workers=max(1, parallel)) as ex:
        futs = {ex.submit(pull_one, tool, img, ctr_namespace): img for img in to_pull}
        for fut in as_completed(futs):
            img = futs[fut]
            try:
                fut.result()
                print(f"pulled: {img}")
            except Exception as e:
                msg = str(e)
                print(f"FAILED pull: {img}\n  reason: {msg}")
                failures.append((img, msg))
                if not ignore_errors:
                    raise RuntimeError(f"pull failed for image: {img}") from e

    if failures and ignore_errors:
        print("\n[WARN] some images failed to pull, but --ignore-pull-errors enabled:")
        for img, msg in failures:
            print(f"  - {img}: {msg}")


def save_images(images: List[str], tool: str, outfile: str, ctr_namespace: str) -> None:
    if not images:
        return
    if tool == "nerdctl":
        cmd = ["nerdctl", "save", "-o", outfile] + images
    elif tool == "docker":
        cmd = ["docker", "save", "-o", outfile] + images
    elif tool == "ctr":
        cmd = ["ctr", "-n", ctr_namespace, "images", "export", outfile] + images
    else:
        raise RuntimeError(f"unsupported image tool: {tool}")
    run(cmd)


def make_metadata(name: str, addon_type: str, version: str, arch: str) -> Dict[str, Any]:
    return {"addons": [{"type": addon_type, "name": name, "version": version, "arch": arch}],
            "kc_versions": []}


def safe_filename(s: str) -> str:
    # 避免出现路径分隔符或奇怪字符导致文件名不合法
    return re.sub(r"[^A-Za-z0-9._+-]+", "_", s)


def resolve_values_path(chart_path: str, values_arg: Optional[str]) -> Optional[str]:
    if values_arg:
        return values_arg
    default_values = os.path.join(chart_path, "values.yaml")
    return default_values if os.path.isfile(default_values) else None


def main():
    p = argparse.ArgumentParser(description="Package kubeclipper addon resource with images")
    p.add_argument("--chart-path", required=True, help="Unpacked Helm chart root")
    # ✅ values 变可选：不传就默认用 chart 里的 values.yaml（如果存在）
    p.add_argument("--values", help="values.yaml path (optional, default: <chart>/values.yaml if exists)")
    p.add_argument("--name", required=True, help="addon name, e.g. cilium")
    p.add_argument("--type", default="cni", help="addon type (cni/csi/cri/app)")
    p.add_argument("--version", required=True, help="addon version, e.g. v1.14.5")
    p.add_argument("--arch", default="amd64", help="arch, e.g. amd64/arm64")
    # ✅ output 可选：不传就按 name-version-arch.tar.gz
    p.add_argument("--output", help="output tar.gz (default: <name>-<version>-<arch>.tar.gz)")

    p.add_argument("--image-tool", default="nerdctl", choices=["nerdctl", "docker", "ctr"],
                   help="tool to pull/save images (default: nerdctl)")
    p.add_argument("--pull", dest="pull", action="store_true", default=True,
                   help="pull images before saving (default: enabled)")
    p.add_argument("--no-pull", dest="pull", action="store_false",
                   help="do not pull images before saving")
    p.add_argument("--parallel", type=int, default=4, help="parallel pull workers (default: 4)")
    p.add_argument("--ctr-namespace", default="k8s.io", help="ctr namespace (default: k8s.io)")
    p.add_argument("--ignore-pull-errors", action="store_true",
                   help="continue even if some images fail to pull")
    p.add_argument("--helm-arg", action="append", default=[],
                   help="extra args passed to 'helm template' (repeatable)")

    args = p.parse_args()

    # ✅ 默认 output：name-version-arch.tar.gz
    if not args.output:
        out_name = f"{safe_filename(args.name)}-{safe_filename(args.version)}-{safe_filename(args.arch)}.tar.gz"
        args.output = out_name

    # ✅ 默认 values：<chart>/values.yaml（存在才用）
    values_path = resolve_values_path(args.chart_path, args.values)

    workdir = tempfile.mkdtemp(prefix="kc-addon-")
    try:
        target_dir = os.path.join(workdir, args.name, args.version, args.arch)
        os.makedirs(target_dir, exist_ok=True)

        chart_tar = os.path.join(target_dir, "chart.tgz")
        tar_dir(args.chart_path, chart_tar)

        # 打包时把 values 拷贝进去（如果存在）
        if values_path:
            shutil.copy(values_path, os.path.join(target_dir, "values.yaml"))

        images = render_images(args.chart_path, values_path, args.helm_arg)

        images_list = os.path.join(target_dir, "images.list")
        with open(images_list, "w") as f:
            f.write("\n".join(images))

        if images and args.pull:
            pull_images(images, args.image_tool, args.parallel, args.ctr_namespace, args.ignore_pull_errors)

        if images:
            images_tar = os.path.join(target_dir, "images.tar")
            save_images(images, args.image_tool, images_tar, args.ctr_namespace)

        meta = make_metadata(args.name, args.type, args.version, args.arch)
        with open(os.path.join(workdir, "metadata.json"), "w") as f:
            json.dump(meta, f, indent=2)

        with tarfile.open(args.output, "w:gz") as out:
            out.add(workdir, arcname=".")
        print("resource package generated:", args.output)
        print("values used:", values_path if values_path else "(none)")
        print("images (%d):" % len(images))
        for img in images:
            print("  ", img)

    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    main()