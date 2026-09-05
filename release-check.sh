#!/bin/sh
# Releases are cut from main only.
#
# stapel-core's 0.60.1 and 0.60.2 were tagged on two branches off 0.60.0 and
# neither was merged back, so the higher version shipped WITHOUT the lower
# one's fix and two fleets had to pin the lower version to keep it. Nothing
# was red: both tags built, both published, and the regression only showed
# up in production. This gate refuses that shape at tag time.
#
# A releasable commit must be contained in main, and it must contain every
# v* tag that already exists — a release that drops an earlier release's
# changes fails here, naming the tags it would drop.
#
#   ./release-check.sh           # check HEAD
#   ./release-check.sh v0.60.3   # check a tag

set -eu

REF="${1:-HEAD}"
COMMIT=$(git rev-parse "${REF}^{commit}")

# main as the remote knows it; the local branch when there is no remote.
if git rev-parse --verify --quiet refs/remotes/origin/main >/dev/null; then
    MAIN_NAME="origin/main"
elif git rev-parse --verify --quiet refs/heads/main >/dev/null; then
    MAIN_NAME="main"
else
    echo "release-check: no main ref to check against (fetch it first)." >&2
    echo "  A gate with nothing to compare against proves nothing, so this is an error." >&2
    exit 1
fi
MAIN=$(git rev-parse "${MAIN_NAME}^{commit}")

failed=0

if ! git merge-base --is-ancestor "$COMMIT" "$MAIN"; then
    echo "release-check: $REF ($COMMIT) is not on $MAIN_NAME." >&2
    echo "  Merge the branch into main and tag the merge; do not tag a side branch." >&2
    failed=1
fi

for tag in $(git tag -l 'v*'); do
    if ! git merge-base --is-ancestor "${tag}^{commit}" "$COMMIT"; then
        echo "release-check: $REF does not contain $tag — that release's changes would be dropped." >&2
        failed=1
    fi
done

if [ "$failed" -ne 0 ]; then
    echo "release-check: FAILED — releases are cut from main only." >&2
    exit 1
fi

echo "release-check: $REF is on $MAIN_NAME and contains all $(git tag -l 'v*' | wc -l | tr -d ' ') v* tags."
