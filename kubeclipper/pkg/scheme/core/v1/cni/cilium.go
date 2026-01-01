package cni

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"time"

	"github.com/kubeclipper/kubeclipper/pkg/component"
	"github.com/kubeclipper/kubeclipper/pkg/component/common"
	v1 "github.com/kubeclipper/kubeclipper/pkg/scheme/core/v1"
	"github.com/kubeclipper/kubeclipper/pkg/simple/downloader"
	"github.com/kubeclipper/kubeclipper/pkg/utils/fileutil"
	"github.com/kubeclipper/kubeclipper/pkg/utils/strutil"
	tmplutil "github.com/kubeclipper/kubeclipper/pkg/utils/template"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

const (
	CiliumNamespaceDefault = "kube-system"
)

func init() {
	Register(&CiliumRunnable{})
	if err := component.RegisterTemplate(fmt.Sprintf(component.RegisterTemplateKeyFormat,
		cniInfo+"-cilium", version, component.TypeTemplate), &CiliumRunnable{}); err != nil {
		panic(err)
	}
	if err := component.RegisterAgentStep(fmt.Sprintf(component.RegisterStepKeyFormat,
		cniInfo+"-cilium", version, component.TypeStep), &CiliumRunnable{}); err != nil {
		panic(err)
	}
}

type CiliumRunnable struct {
	BaseCni
	CiliumConfig *v1.Cilium
}

func (runnable *CiliumRunnable) Type() string {
	return "cilium"
}

func (runnable *CiliumRunnable) Create() Stepper {
	return &CiliumRunnable{}
}

func (runnable *CiliumRunnable) NewInstance() component.ObjectMeta {
	return &CiliumRunnable{}
}

func (runnable *CiliumRunnable) InitStep(metadata *component.ExtraMetadata, cni *v1.CNI, networking *v1.Networking) Stepper {
	stepper := &CiliumRunnable{}
	stepper.CNI = *cni
	stepper.LocalRegistry = cni.LocalRegistry
	stepper.BaseCni.Type = "cilium"
	stepper.Version = cni.Version
	stepper.CriType = metadata.CRI
	stepper.Offline = cni.Offline
	stepper.Namespace = cni.Namespace
	stepper.CiliumConfig = cni.Cilium
	if stepper.Namespace == "" {
		stepper.Namespace = CiliumNamespaceDefault
	}
	return stepper
}

func (runnable *CiliumRunnable) LoadImage(nodes []v1.StepNode) ([]v1.Step, error) {
	var steps []v1.Step
	bytes, err := json.Marshal(runnable)
	if err != nil {
		return nil, err
	}

	if runnable.Offline && runnable.LocalRegistry == "" {
		return []v1.Step{LoadImage("cilium", bytes, nodes)}, nil
	}

	return steps, nil
}

func (runnable *CiliumRunnable) InstallSteps(nodes []v1.StepNode, kubernetesVersion string) ([]v1.Step, error) {
	var steps []v1.Step
	bytes, err := json.Marshal(runnable)
	if err != nil {
		return nil, err
	}
	chart := &common.Chart{
		PkgName: "cilium",
		Version: runnable.Version,
		Offline: runnable.Offline,
	}

	cLoadSteps, err := chart.InstallStepsV2(nodes)
	if err != nil {
		return nil, err
	}
	steps = append(steps, cLoadSteps...)
	steps = append(steps, RenderYaml("cilium", bytes, nodes))
	steps = append(steps, InstallCiliumRelease(filepath.Join(downloader.BaseDstDir, "."+chart.PkgName, chart.Version, downloader.ChartFilename), filepath.Join(manifestDir, "cilium.yaml"), runnable.Namespace, nodes))

	return steps, nil
}

func (runnable *CiliumRunnable) UninstallSteps(nodes []v1.StepNode) ([]v1.Step, error) {
	bytes, err := json.Marshal(runnable)
	if err != nil {
		return nil, err
	}
	var steps []v1.Step
	if runnable.Offline && runnable.LocalRegistry == "" {
		steps = append(steps, RemoveImage("cilium", bytes, nodes))
	}
	steps = append(steps, v1.Step{
		ID:         strutil.GetUUID(),
		Name:       "uninstallCiliumRelease",
		Timeout:    metav1.Duration{Duration: 1 * time.Minute},
		ErrIgnore:  true,
		RetryTimes: 1,
		Nodes:      nodes,
		Action:     v1.ActionUninstall,
		Commands: []v1.Command{
			{
				Type:         v1.CommandShell,
				ShellCommand: []string{"helm", "uninstall", "cilium", "-n", runnable.Namespace},
			},
		},
	})
	return steps, nil
}

func (runnable *CiliumRunnable) CmdList(namespace string) map[string]string {
	cmdList := make(map[string]string)
	cmdList["get"] = fmt.Sprintf("kubectl get po -n %s | grep cilium", namespace)
	cmdList["restart"] = fmt.Sprintf("kubectl rollout restart ds cilium -n %s", namespace)

	return cmdList
}

func (runnable *CiliumRunnable) Render(ctx context.Context, opts component.Options) error {
	if err := os.MkdirAll(manifestDir, 0755); err != nil {
		return err
	}
	manifestFile := filepath.Join(manifestDir, "cilium.yaml")
	return fileutil.WriteFileWithContext(ctx, manifestFile, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, 0644,
		runnable.renderCiliumTo, opts.DryRun)
}

func (runnable *CiliumRunnable) renderCiliumTo(w io.Writer) error {
	at := tmplutil.New()
	ciliumTemp, err := runnable.CiliumTemplate()
	if err != nil {
		return err
	}
	if _, err := at.RenderTo(w, ciliumTemp, runnable); err != nil {
		return err
	}
	return nil
}

func (runnable *CiliumRunnable) CiliumTemplate() (string, error) {
	return ciliumValuesTemplate, nil
}

// InstallCiliumRelease apply helm chart with rendered values
func InstallCiliumRelease(chartPath string, values string, namespace string, nodes []v1.StepNode) v1.Step {
	return v1.Step{
		ID:         strutil.GetUUID(),
		Name:       "installCiliumRelease",
		Timeout:    metav1.Duration{Duration: 2 * time.Minute},
		ErrIgnore:  false,
		RetryTimes: 1,
		Nodes:      nodes,
		Commands: []v1.Command{
			{
				Type:         v1.CommandShell,
				ShellCommand: []string{"helm", "upgrade", "--install", "--create-namespace", "cilium", "-n", namespace, chartPath, "-f", values},
			},
		},
	}
}

const ciliumValuesTemplate = `operator:
  replicas: {{ if .CiliumConfig }}{{.CiliumConfig.OperatorReplicas}}{{else}}1{{end}}
ipam:
  operator:
    clusterPoolIPv4PodCIDRList: {{ if .CiliumConfig }}{{ toJson .CiliumConfig.ClusterPoolIPv4PodCIDRList }}{{else}}["192.168.64.0/18"]{{end}}
    clusterPoolIPv4MaskSize: {{ if .CiliumConfig }}{{.CiliumConfig.ClusterPoolIPv4MaskSize}}{{else}}25{{end}}
kubeProxyReplacement: "{{ if .CiliumConfig }}{{.CiliumConfig.KubeProxyReplacement}}{{else}}false{{end}}"
`
