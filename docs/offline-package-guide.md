# 制作并上传特定 Kubernetes 版本的离线包

本文档说明如何针对指定 Kubernetes 版本手工构建符合 KubeClipper 要求的离线资源包，并使用 `kcctl` 上传到控制面。流程基于源码中的 CLI 校验规则和下载器实现整理。

## 离线包命名与目录结构

`kcctl resource push` 期望的离线包格式在源码中明确了命名规则和目录层级：包名需遵循 `name-version-arch.tar.gz`，压缩包内部目录为 `name/version/arch/`，包含资源文件和 `manifest.json` 用于校验。【F:pkg/cli/resource/resource.go†L69-L92】【F:pkg/simple/downloader/options.go†L24-L35】

以 Kubernetes v1.27.3、amd64 架构为例，打包前目录应为：

```
k8s/
  v1.27.3/
    amd64/
      images.tar.gz       # 必需，K8s 及依赖镜像集合
      charts.tgz          # 如需额外 Helm charts 可包含
      configs.tar.gz      # 可选，组件额外配置集合
      manifest.json       # 必需，记录上面文件的 MD5 校验
      config/
        manifest.json     # 如果包含 configs.tar.gz，需在此描述展开后的文件校验
```

> `manifest.json` 内容是一个数组，每个元素包含 `name`、`digest`（MD5）、`path` 字段，对应下载器校验逻辑。`path` 为文件所在目录前缀，`name` 为文件名，`digest` 为 `md5sum` 结果。【F:pkg/simple/downloader/options.go†L30-L35】【F:pkg/simple/downloader/downloader.go†L214-L277】

## 构建离线包步骤

1. **准备镜像归档**：将所需 Kubernetes 组件镜像（kube-apiserver、kube-controller-manager、kube-scheduler、kube-proxy、pause、coredns 等以及所选 CRI/CNI/CSI 依赖）执行 `docker save` 或 `ctr images export` 打成 `images.tar.gz`，放入 `k8s/v1.27.3/amd64/`。
2. **准备 Charts/配置（如需）**：若集群安装需要额外 Helm chart 或配置文件，将其分别压缩为 `charts.tgz` 与 `configs.tar.gz`，并放入同一路径。
3. **生成 manifest.json**：在 `k8s/v1.27.3/amd64/` 内执行 `md5sum images.tar.gz [charts.tgz] [configs.tar.gz] > /tmp/md5.list`，然后将结果转换为以下 JSON 结构并保存为 `manifest.json`：
   ```json
   [
     {"name":"images.tar.gz","digest":"<md5值>","path":"/tmp/kc-downloader/.k8s/v1.27.3/amd64"},
     {"name":"charts.tgz","digest":"<md5值>","path":"/tmp/kc-downloader/.k8s/v1.27.3/amd64"},
     {"name":"configs.tar.gz","digest":"<md5值>","path":"/tmp/kc-downloader/.k8s/v1.27.3/amd64"}
   ]
   ```
   - `path` 字段可以填写解压后的目标目录，下载器会用它拼接完整路径进行 MD5 校验。【F:pkg/simple/downloader/downloader.go†L214-L277】
   - 如果提供了 `configs.tar.gz`，解压后的每个文件同样需要在 `k8s/v1.27.3/amd64/config/manifest.json` 中列出对应 MD5。
4. **打包**：在 `k8s/` 目录外执行 `tar -czvf k8s-v1.27.3-amd64.tar.gz k8s/`，生成最终离线包。

## 上传到 KubeClipper

1. **准备 kcctl 访问配置**：`kcctl resource push` 会读取部署配置以通过 SSH 与控制面通信，确保 `~/.kc/config`（或 `--config` 指定的文件）内已配置控制节点地址及 SSH 密钥/密码，否则命令会拒绝执行。【F:pkg/cli/resource/resource.go†L228-L255】
2. **上传离线包**：在可访问控制面的环境中执行：
   ```bash
   kcctl resource push \
     --type k8s \
     --pkg /path/to/k8s-v1.27.3-amd64.tar.gz
   ```
   - `--type` 必填，用于区分资源类型（Kubernetes 组件使用 `k8s`）。
   - `--pkg` 指向本地打好的离线包。
3. **验证上传结果**：可通过 `kcctl resource list --type k8s --name k8s --version v1.27.3` 查看服务器上的离线包元数据，确认上传成功并可用于离线安装或升级。【F:pkg/cli/resource/resource.go†L37-L108】【F:pkg/cli/resource/resource.go†L160-L209】

按照以上步骤即可为特定 Kubernetes 版本制作符合 KubeClipper 期望的离线资源包，并安全上传到控制面以供离线部署或升级使用。

## 使用脚本自动化打包与上传

仓库提供 `scripts/package-k8s-offline.sh`，可按上述目录结构自动生成 `manifest.json`、压缩 tar 包，并可选执行 `kcctl resource push` 完成上传。【F:scripts/package-k8s-offline.sh†L1-L178】

### 方式一：直接拉取镜像打包（无需提前准备 images.tar.gz）

脚本会自动：下载匹配版本与架构的 `kubeadm` 二进制、调用 `kubeadm config images list` 获取所需核心镜像、使用 Docker/ctr/nerdctl 拉取镜像并导出为 `images.tar.gz`。

```bash
scripts/package-k8s-offline.sh \
  --version v1.27.3 \
  --arch amd64 \
  --pull-tool docker \  # 或 ctr、nerdctl
  --image-repo registry.k8s.io \  # 可覆写镜像仓库
  --extra-image registry.k8s.io/pause:3.9 \  # 可重复添加附加镜像
  --output-dir /data/out \
  --push \
  --kcctl-config ~/.kc/config
```

### 方式二：使用已有 images.tar.gz

```bash
scripts/package-k8s-offline.sh \
  --version v1.27.3 \
  --arch amd64 \
  --images /data/images.tar.gz \
  --charts /data/charts.tgz \
  --configs-dir /data/configs \
  --output-dir /data/out \
  --push \
  --kcctl-config ~/.kc/config
```

- 未指定 `--images` 时，脚本会自动拉取镜像并生成 `images.tar.gz`，默认使用 docker 拉取，也可通过 `--pull-tool ctr` 或 `--pull-tool nerdctl` 改用对应工具。【F:scripts/package-k8s-offline.sh†L55-L106】【F:scripts/package-k8s-offline.sh†L144-L170】
- 可通过 `--image-repo` 覆盖 kubeadm 查询镜像的仓库地址，或用 `--extra-image` 追加自定义镜像到离线包内。【F:scripts/package-k8s-offline.sh†L60-L104】
- `--charts`、`--configs-dir` 可选，会被打包并写入 manifest；`--push` 开启后会调用 `kcctl resource push --type k8s --pkg <tar>`，`--kcctl-config` 可显式指定配置文件。【F:scripts/package-k8s-offline.sh†L172-L207】

执行成功后，会在 `--output-dir`（默认当前目录）生成 `k8s-<version>-<arch>.tar.gz`，可直接在控制面使用或通过脚本自动上传。【F:scripts/package-k8s-offline.sh†L186-L207】

## 关于 tarball-kubernetes.sh 辅助脚本

项目并未在源码中携带 `tarball-kubernetes.sh`，但在开发者指南中给出了使用示例（通过外部链接下载）以快速生成 `k8s/<version>/<arch>/` 目录及清单文件。【F:docs/dev-guide.md†L309-L333】如需脚本化生成，可以按照该示例下载脚本并执行；如果无法获取脚本，则可完全按照本文档的手工步骤自行打包，生成的目录结构和 `manifest.json` 即可满足 `kcctl` 的校验要求。
