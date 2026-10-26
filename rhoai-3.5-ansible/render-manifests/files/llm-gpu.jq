# Runtime counterpart of check_gpu_capacity.yml, check_gpu_available.yml and
# serve_model.yml. Tests compare these decisions with the real Ansible expressions.
def main_container: [.spec.template.containers[]? | select(.name == "main")][0] // {};
def selected($cfg):
  (.metadata.labels // {}) as $labels |
  (.spec.unschedulable != true) and
  all(($cfg.node_selector // {} | to_entries)[]; $labels[.key] == .value);
def capacity($cfg):
  [.items[] | select(selected($cfg)) |
   ((.status.allocatable[$cfg.gpu_resource] // "0") | tonumber)] | add // 0;
def slots($cfg):
  [.items[] | select(selected($cfg)) |
   (((.status.allocatable[$cfg.gpu_resource] // "0") | tonumber) / ($cfg.gpu_count | tonumber) | floor)] | add // 0;
def normalized_spec:
  . as $lis | main_container as $c |
  {image: ($c.image // ""), args: ($c.args // []), env: ($c.env // []),
   resources: ($c.resources // {}), volumeMounts: ($c.volumeMounts // []),
   startupProbe: ($c.startupProbe // {}),
   nodeSelector: ($lis.spec.template.nodeSelector // {}),
   volumes: ($lis.spec.template.volumes // []), replicas: ($lis.spec.replicas // 1),
   uri: ($lis.spec.model.uri // "")};
def request($r): (.resources.requests[$r] // .resources.limits[$r] // 0) | tonumber;
def reserved($r):
  ([.spec.containers[]? | request($r)] | add // 0) as $app |
  (reduce .spec.initContainers[]? as $c ({sidecars: 0, peak: 0};
    ($c | request($r)) as $count |
    if $c.restartPolicy == "Always" then
      .sidecars += $count | .peak = ([.peak, .sidecars] | max)
    else .peak = ([.peak, (.sidecars + $count)] | max) end)) as $init |
  [($app + $init.sidecars), $init.peak] | max;
def availability($cfg; $pods):
  [ .items[] | select(selected($cfg)) | . as $node |
    [$pods.items[] |
      select(.spec.nodeName == $node.metadata.name and
             .status.phase != "Succeeded" and .status.phase != "Failed") |
      {pod: (.metadata.namespace + "/" + .metadata.name), count: reserved($cfg.gpu_resource)}
    ] as $used |
    {slots: (([0, (((.status.allocatable[$cfg.gpu_resource] // "0") | tonumber)
                 - ([$used[].count] | add // 0))] | max) / ($cfg.gpu_count | tonumber) | floor),
     pods: [$used[] | select(.count > 0) | .pod]}
  ] | {slots: ([.[].slots] | add // 0), pods: [.[].pods[]]};
def config:
  main_container as $c |
  [($c.resources.requests // {}) | keys[] | select(test("^nvidia[.]com/(gpu|mig-.*)$"))] as $keys |
  if ($keys | length) != 1 then error("Expected exactly one GPU resource") else
    {name: .metadata.name, namespace: .metadata.namespace,
     gpu_resource: $keys[0], gpu_count: ($c.resources.requests[$keys[0]] | tonumber),
     replicas: (.spec.replicas // 1), node_selector: (.spec.template.nodeSelector // {})}
  end;
def validate_nodes:
  if (.items | type) != "array" then error("Invalid NodeList") else . end;
def preflight($models):
  . as $nodes |
  (reduce $models[] as $cfg ({};
    .[$cfg.gpu_resource] = ((.[$cfg.gpu_resource] // 0) +
      (($cfg.gpu_count | tonumber) * ($cfg.replicas | tonumber))))) as $demands |
  [$demands | to_entries[] | . as $d |
   ($nodes | capacity({gpu_resource: $d.key})) as $cap |
   select($d.value > $cap) | "GPU insufficient: \($d.key) required=\($d.value) allocatable=\($cap)"] +
  [$models[] | . as $cfg | ($nodes | slots($cfg)) as $slots |
   select($slots < ($cfg.replicas | tonumber)) |
   "Model \($cfg.name) does not fit selected nodes: slots=\($slots) replicas=\($cfg.replicas)"] |
  if length > 0 then error(join("; ")) else true end;
def plan($desired; $live):
  ($desired | config) as $cfg |
  if $live == null then "create"
  elif ($desired | normalized_spec) == ($live | normalized_spec) then "unchanged"
  else capacity($cfg) as $cap |
    if $cap <= 0 then error("Cannot determine GPU capacity; refusing to delete the existing model")
    elif ($cap < (($cfg.gpu_count | tonumber) * ($cfg.replicas | tonumber) * 2)) or
         (($live.spec.template.nodeSelector // {}) != ($desired.spec.template.nodeSelector // {})) or
         ([$live | main_container | (.resources.requests // {}) | keys[] |
            select(test("^nvidia[.]com/(gpu|mig-.*)$"))] != [$cfg.gpu_resource])
    then "recreate" else "update" end
  end;
