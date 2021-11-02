#!/usr/bin/python3 -B

# Copyright 2021 The Android Open Source Project
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Read the EXPECTED_UPSTREAM and update the files from the upstream."""

import logging
# pylint: disable=g-importing-member
from pathlib import Path
import sys
from typing import List

# pylint: disable=g-multiple-import
from common_util import (
    ExpectedUpstreamEntry,
    ExpectedUpstreamFile,
    has_file_in_tree,
    LIBCORE_DIR,
)

from git import (
    Blob,
    IndexFile,
    Repo,
)

# Enable INFO logging for error emitted by GitPython
logging.basicConfig(level=logging.INFO)

# Pick an arbitrary existing commit with an empty tree
EMPTY_COMMIT_SHA = "d85bc16ba1cdcc20bec6fcbfe46dc90f9fcd2f78"


def validate_and_remove_updated_entries(
    entries: List[ExpectedUpstreamEntry],
    repo: Repo) -> List[ExpectedUpstreamEntry]:
  """Returns a list of entries of which the file content needs to be updated."""
  head_tree = repo.head.commit.tree
  result: List[ExpectedUpstreamEntry] = []

  for e in entries:
    try:
      # The following step validate each entry by querying the git database
      commit = repo.commit(e.git_ref)
      source_blob = commit.tree.join(e.src_path)
      if not has_file_in_tree(e.dst_path, head_tree):
        # Add the entry if the file is missing in the HEAD
        result.append(e)
        continue

      dst_blob = head_tree.join(e.dst_path)
      # Add the entry if the content is different.
      # data_stream will be close during GC.
      if source_blob.data_stream.read() != dst_blob.data_stream.read():
        result.append(e)
    except:
      print(f"ERROR: reading entry: {e}", file=sys.stderr)
      raise

  return result


def partition_entries_by_ref(
    entries: List[ExpectedUpstreamEntry]) -> List[List[ExpectedUpstreamEntry]]:
  result_map = {}
  for e in entries:
    if result_map.get(e.git_ref) is None:
      result_map[e.git_ref] = []
    result_map[e.git_ref].append(e)

  return list(result_map.values())


THIS_TOOL_PATH = Path(__file__).relative_to(LIBCORE_DIR)
MSG_FIRST_COMMIT = ("Import {summary} from {ref}\n"
                    "\n"
                    "List of files:\n"
                    "  {files}\n"
                    "\n"
                    f"Generated by {THIS_TOOL_PATH}"
                    "\n"
                    "Test: N/A")

MSG_SECOND_COMMIT = ("Merge {summary} from {ref} into the "
                     " expected_upstream branch\n"
                     "\n"
                     "List of files:\n"
                     "  {files}\n"
                     "\n"
                     f"Generated by {THIS_TOOL_PATH}"
                     "\n"
                     "Test: N/A")


def merge_files_and_create_commit(entry_set: List[ExpectedUpstreamEntry],
                                  repo: Repo) -> None:
  r"""Create the commits importing the given files into the current branch.

  `--------<ref>---------------   aosp/upstream_openjdkXXX
             \
        <first_commit>
              \
  -------<second_commit>------   expected_upstream

  This function creates the 2 commits, i.e. first_commit and second_commit, in
  the diagram. The goal is to checkout a subset files specified in the
  entry_set, and merged into the pected_upstream branch in order to keep the
  git-blame history of the individual files. first_commit is needed in order
  to move the files specified in the entry_set.

  In the implementation, first_commit isn't really modified from the ref, but
  created from an empty tree, and all files in entry_set will be added into
  the first_commit, second_commit is a merged commit and modified from
  the parent in the expected_upstream branch, and any file contents in the
  first commit will override the file content in the second commit.

  You may reference the following git commands for understanding which should
  create the same commits, but the python implementation is cleaner, because
  it doesn't change the working tree or create a new branch.
  first_commit:
      git checkout -b temp_branch <entry.git_ref>
      rm -r * .jcheck/ .hgignore .hgtags # Remove hidden files
      git checkout <entry.git_ref> <entry.src_path>
      mkdir -p <entry.dst_path>.directory && git mv <entry.src_path>
      <entry.dst_path>
      git commit -a
  second_commit:
      git merge temp_branch
      git checkout HEAD -- ojluni/ # Force checkout to resolve merge conflict
      git checkout temp_branch -- <entry.dst_path>
      git commit

  Args:
    entry_set: a list of entries
    repo: the repository object
  """
  ref = entry_set[0].git_ref
  upstream_commit = repo.commit(ref)

  # We need an index empty initially, i.e. no staged files.
  # Note that the empty commit is not the parent. The parents can be set later.
  first_index = IndexFile.from_tree(repo, repo.commit(EMPTY_COMMIT_SHA))
  for entry in entry_set:
    src_blob = upstream_commit.tree[entry.src_path]
    # Write into the file system directly because GitPython provides no API
    # writing into the index in memory. IndexFile.move doesn't help here,
    # because the API requires the file on the working tree too.
    # However, it's fine, because we later reset the HEAD to the second commit.
    # The user expects the file showing in the file system, and the file is
    # not staged/untracked because the file is in the second commit too.
    Path(entry.dst_path).parent.mkdir(parents=True, exist_ok=True)
    with open(entry.dst_path, "wb") as file:
      file.write(src_blob.data_stream.read())
    first_index.add(entry.dst_path)

  dst_paths = [e.dst_path for e in entry_set]
  str_dst_paths = "\n  ".join(dst_paths)
  summary_msg = "files"
  if len(entry_set) == 1:
    summary_msg = Path(entry_set[0].dst_path).stem
  msg = MSG_FIRST_COMMIT.format(
      summary=summary_msg, ref=ref, files=str_dst_paths)

  first_commit = first_index.commit(
      message=msg, parent_commits=[upstream_commit], head=False)

  # The second commit is a merge commit. It doesn't use the current index,
  # i.e. repo.index, to avoid affecting the current staged files.
  prev_head = repo.active_branch.commit
  second_index = IndexFile.from_tree(repo, prev_head)
  blob_filter = lambda obj, i: isinstance(obj, Blob)
  blobs = first_commit.tree.traverse(blob_filter)
  second_index.add(blobs)
  msg = MSG_SECOND_COMMIT.format(
      summary=summary_msg, ref=ref, files=str_dst_paths)
  second_commit = second_index.commit(
      message=msg, parent_commits=[prev_head, first_commit], head=True)

  # We updated the HEAD to the second commit. Thus, git-reset updates the
  # current index. Otherwise, the current index, aka, repo.index, shows that
  # the files are deleted.
  repo.index.reset(paths=dst_paths)

  print(f"New merge commit {second_commit} contains:")
  print(f"  {str_dst_paths}")


def create_commits(repo: Repo) -> None:
  """Create the commits importing files according to the EXPECTED_UPSTREAM."""
  current_tracking_branch = repo.active_branch.tracking_branch()
  if current_tracking_branch.name != "aosp/expected_upstream":
    print("This script should only run on aosp/expected_upstream branch. "
          f"Currently, this is on branch {repo.active_branch} "
          f"tracking {current_tracking_branch}")

  print("Reading EXPECTED_UPSTREAM file...")
  expected_upstream_entries = ExpectedUpstreamFile().read_all_entries()

  outdated_entries = validate_and_remove_updated_entries(
      expected_upstream_entries, repo)

  if not outdated_entries:
    print("No need to update. All files are updated.")
    return

  print("The following entries will be updated from upstream")
  for e in outdated_entries:
    print(f"  {e.dst_path}")

  entry_sets_to_be_merged = partition_entries_by_ref(outdated_entries)

  for entry_set in entry_sets_to_be_merged:
    merge_files_and_create_commit(entry_set, repo)


def main():
  repo = Repo(LIBCORE_DIR.as_posix())
  try:
    create_commits(repo)
  finally:
    repo.close()


if __name__ == "__main__":
  main()
