# This is the Python package directory, not the repo root — the real Makefile is
# one level up. They share a name (sonde/sonde), so `cd sonde` from the repo
# root then `make deploy` is an easy mistake that otherwise fails with a bare
# "No rule to make target". Forward every target instead.
.DEFAULT_GOAL := deploy

%:
	@echo "note: running from the repo root ($(abspath ..))"
	@$(MAKE) --no-print-directory -C .. $@
