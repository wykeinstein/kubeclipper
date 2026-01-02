#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import argparse
import gzip
import hashlib
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


def md5sum(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def safe_filename(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._+-]+", "_", s)


def tar_dir_as_tgz(src_dir: str, dst_tgz: str) -> None:
    """
    把目录打成 .tgz（tar.gz），归档内包含目录名本身（basename）。
    输出文件名按 KubeClipper 资源习惯：charts.tgz
    """
    with tarfile.open(dst_tgz, "w:gz") as tar:
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
        # 1) 标准 Pod spec container image
        for k in ("containers", "initContainers", "ephemeralContainers"):
            v = obj.get(k)
            if isinstance(v, list):
                for c in v:
                    if isinstance(c, dict) and isinstance(c.get("image"), str):
                        img = c["image"].strip().strip('"').strip("'")
                        if img and img not in seen:
                            out.append(img)
                            seen.add(img)

        # 2) chart values 常见 image: {repository, tag} 结构
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

    # fallback regex
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
            # ctr ls 输出可能包含 digest / 展示形式不同，做 contains
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


def save_images_to_tar(images: List[str], tool: str, outfile_tar: str, ctr_namespace: str) -> None:
    if not images:
        return
    if tool == "nerdctl":
        cmd = ["nerdctl", "save", "-o", outfile_tar] + images
    elif tool == "docker":
        cmd = ["docker", "save", "-o", outfile_tar] + images
    elif tool == "ctr":
        cmd = ["ctr", "-n", ctr_namespace, "images", "export", outfile_tar] + images
    else:
        raise RuntimeError(f"unsupported image tool: {tool}")
    run(cmd)


def gzip_file(src: str, dst_gz: str) -> None:
    with open(src, "rb") as f_in, gzip.open(dst_gz, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)


def resolve_values_path(chart_path: str, values_arg: Optional[str]) -> Optional[str]:
    if values_arg:
        return values_arg
    default_values = os.path.join(chart_path, "values.yaml")
    return default_values if os.path.isfile(default_values) else None


def write_manifest(arch_dir: str, version: str, arch: str, filenames: List[str]) -> None:
    """
    在 <...>/<version>/<arch>/manifest.json 写入：
    [
      {"name": "...", "digest": "<md5>", "path": "vX.Y.Z/amd64"},
      ...
    ]
    """
    rel_path = f"{version}/{arch}"
    arr = []
    for fn in filenames:
        fp = os.path.join(arch_dir, fn)
        if not os.path.isfile(fp):
            continue
        arr.append({
            "name": fn,
            "digest": md5sum(fp),
            "path": rel_path,
        })
    manifest_path = os.path.join(arch_dir, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(arr, f, indent=2, ensure_ascii=False)


def create_final_tarball(output: str, top_dir: str) -> None:
    """
    关键点：tar 内顶层必须是 <name>/...，不能是 '.'，否则 kcctl after-hook 会 rm -rf '.'
    这里用 arcname=basename(top_dir) 来保证顶层目录正确。
    """
    base = os.path.basename(top_dir.rstrip("/"))
    with tarfile.open(output, "w:gz") as out:
        out.add(top_dir, arcname=base)


def main():
    p = argparse.ArgumentParser(description="Package kubeclipper addon resource with images (manifest.json style)")
    p.add_argument("--chart-path", required=True, help="Unpacked Helm chart root")
    p.add_argument("--values", help="values.yaml path (optional, default: <chart>/values.yaml if exists)")
    p.add_argument("--name", required=True, help="addon name, e.g. calico/cilium")
    p.add_argument("--type", default="cni", help="addon type (cni/csi/cri/app) [currently not used in manifest mode]")
    p.add_argument("--version", required=True, help="addon version, e.g. v3.26.1")
    p.add_argument("--arch", default="amd64", help="arch, e.g. amd64/arm64")
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

    if not args.output:
        args.output = f"{safe_filename(args.name)}-{safe_filename(args.version)}-{safe_filename(args.arch)}.tar.gz"

    values_path = resolve_values_path(args.chart_path, args.values)

    workdir = tempfile.mkdtemp(prefix="kc-addon-")
    try:
        # 最终包的顶层目录必须是 <name>/...
        top_dir = os.path.join(workdir, args.name)
        arch_dir = os.path.join(top_dir, args.version, args.arch)
        os.makedirs(arch_dir, exist_ok=True)

        # 1) 打 charts.tgz
        charts_tgz = os.path.join(arch_dir, "charts.tgz")
        tar_dir_as_tgz(args.chart_path, charts_tgz)

        # 2) 渲染镜像列表
        images = render_images(args.chart_path, values_path, args.helm_arg)

        # 3) 写 images.list（可选，但很有用）
        images_list_path = os.path.join(arch_dir, "images.list")
        with open(images_list_path, "w", encoding="utf-8") as f:
            f.write("\n".join(images))

        # 4) 先 pull 再 save
        if images and args.pull:
            pull_images(images, args.image_tool, args.parallel, args.ctr_namespace, args.ignore_pull_errors)

        # 5) 保存 images.tar.gz（注意：manifest 示例是 tar.gz）
        images_targz = os.path.join(arch_dir, "images.tar.gz")
        if images:
            tmp_tar = os.path.join(arch_dir, "images.tar")
            save_images_to_tar(images, args.image_tool, tmp_tar, args.ctr_namespace)
            gzip_file(tmp_tar, images_targz)
            os.remove(tmp_tar)
        else:
            # 没镜像也可以不生成 images.tar.gz（按需）
            pass

        # 6) 生成 manifest.json（在 arch_dir 下）
        #    按你的 calico 示例：只列 charts.tgz 和 images.tar.gz
        manifest_files = ["charts.tgz"]
        if os.path.isfile(images_targz):
            manifest_files.append("images.tar.gz")
        write_manifest(arch_dir, args.version, args.arch, manifest_files)

        # 7) 打最终资源包（顶层是 <name>/，绝对不能是 '.'）
        create_final_tarball(args.output, top_dir)

        print("resource package generated:", args.output)
        print("values used for rendering:", values_path if values_path else "(none)")
        print("images (%d):" % len(images))
        for img in images:
            print("  ", img)

        print("\npackage layout (top-level):")
        print(f"  {args.name}/{args.version}/{args.arch}/charts.tgz")
        if os.path.isfile(images_targz):
            print(f"  {args.name}/{args.version}/{args.arch}/images.tar.gz")
        print(f"  {args.name}/{args.version}/{args.arch}/manifest.json")

    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    main()
