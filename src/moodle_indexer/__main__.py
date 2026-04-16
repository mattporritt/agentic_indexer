# Copyright (c) Moodle Pty Ltd. All rights reserved.
# Licensed under the Moodle Community License v1.3.
# See LICENSE.md in the repository root for full terms.
# Commercial use requires a separate written agreement with Moodle.

"""Module entrypoint for ``python -m moodle_indexer``."""

from moodle_indexer.cli import main


if __name__ == "__main__":
    main()
