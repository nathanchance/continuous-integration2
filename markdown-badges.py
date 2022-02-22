#!/usr/bin/env python3

from utils import get_config, get_repo_ref


def tree_uses_llvm_version(config, tree, llvm_version):
    repo, ref = get_repo_ref(config, tree)
    for build in config["builds"]:
        if build["git_repo"] == repo and \
           build["git_ref"] == ref and \
           build["llvm_version"] == llvm_version:
            return True
    return False


if __name__ == "__main__":
    config = get_config()
    for tree in config["trees"]:
        tree_name = tree["name"]
        llvm_versions = []

        for llvm_version in config["llvm_versions"]:
            if tree_uses_llvm_version(config, tree_name, llvm_version):
                if llvm_version not in llvm_versions:
                    llvm_versions.append(llvm_version)

        for llvm_version in llvm_versions:
            workflow_url = "https://github.com/clangbuiltlinux/continuous-integration2/actions/workflows/{}-clang-{}.yml".format(
                tree_name, llvm_version)
            print("[![Actions Status]({}/badge.svg)]({})".format(
                workflow_url, workflow_url))
        print()
