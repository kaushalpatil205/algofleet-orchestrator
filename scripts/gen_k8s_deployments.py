import json
import os

VARIANTS_PATH = "variants/variants.json"
BASE_YAML_PATH = "kubernetes/strategies/base/deployment.yaml"

def generate():
    with open(VARIANTS_PATH, "r") as f:
        variants = json.load(f)
        
    with open(BASE_YAML_PATH, "r") as f:
        base_yaml = f.read()

    for family, variant_list in variants.items():
        for v in variant_list:
            name = v["file"].replace("_", "-")
            out_dir = f"kubernetes/strategies/{name}"
            os.makedirs(out_dir, exist_ok=True)

            yaml = base_yaml \
                .replace("STRATEGY_NAME", name) \
                .replace("STRATEGY_SCRIPT_VALUE", v["strategy_script"]) \
                .replace("MAGIC_VALUE", str(v["magic_shadow"]))

            with open(f"{out_dir}/deployment.yaml", "w") as f:
                f.write(yaml)
            
            print(f"Generated K8s deployment for {name}")

if __name__ == "__main__":
    generate()

