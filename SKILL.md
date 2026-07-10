---
name: moleditpy-mcp
description: >
  Drive the MoleditPy molecular editor through its MCP server tools.
  Use when the MoleditPy MCP tools are connected and the task involves
  molecules loaded in MoleditPy: querying structure (SMILES, formula, atoms,
  bonds, 3D coordinates), loading/editing molecules, 3D visualization and
  highlighting, generating quantum-chemistry input files (ORCA, Gaussian,
  GAMESS, NWChem, xTB, ...), sandboxed file I/O, or writing MoleditPy plugins.
  Triggers: "current molecule", "in MoleditPy", "generate an input file",
  "xyz coordinates", "highlight atoms", "write a plugin".
---

# Driving MoleditPy via MCP

MoleditPy exposes its editor over a local MCP HTTP server (default port
`7891`, started from **Plugins → MCP Server** inside the app). All tool
calls run on the app's Qt main thread and act on the molecule the user
currently sees.

## What MoleditPy is

MoleditPy is a desktop molecular editor (PyQt6 + RDKit + PyVista) for
preparing quantum-chemistry / DFT calculations. Its core workflow:

1. **2D editing** — the user draws or loads a molecule on a 2D canvas
   (the primary editing surface; bonds, charges, and stereochemistry are
   edited here). Loading tools (`load_molecule_from_smiles`,
   `load_molecule_by_name`, `load_from_mol_block`) target this canvas.
2. **2D → 3D conversion** — `trigger_3d_conversion` builds a 3D
   structure (RDKit ETKDG embedding + force-field optimization) and
   switches to the 3D viewer. Only after this do 3D coordinates exist.
3. **3D viewing/analysis** — a PyVista-based viewer shows the conformer;
   plugins (and these MCP tools) read coordinates, color atoms/bonds,
   and export geometry from here.
4. **Input generation & plugins** — the geometry feeds input generators
   for Gaussian, ORCA, GAMESS, xTB, etc.; a large official plugin
   ecosystem covers analysis, import/export, and visualization.

The 2D canvas and the 3D viewer hold SEPARATE representations. Most
coordinate-reading MCP tools (including `current molecule` state) see
the **3D side** — so with a freshly drawn 2D molecule, coordinate tools
report "no 3D data" until `trigger_3d_conversion` runs. Editing tools
(`apply_reaction_smarts`, SMILES loading) act on the **2D side** and
require re-conversion for the 3D geometry to reflect the change.

## Core rules

1. **Never retype coordinates.** To create any file that contains the
   current molecule's geometry (QM input files, `.xyz` exports), use
   `write_file_with_xyz_block` — it takes the coordinate block directly
   from the live molecule. Compose the rest of the file with its
   `header`/`footer` arguments. Only use `write_text_file` for files
   that contain no molecular coordinates.
2. **Check state before acting.** `get_current_molecule` tells you if a
   molecule is loaded and whether 3D coordinates exist. If not,
   `trigger_3d_conversion` first — coordinate tools error without a 3D
   conformer.
3. **Checkpoint AFTER edits.** Call `push_undo_checkpoint` after you
   finish modifying the molecule (`apply_reaction_smarts`, `run_python`
   mutations, ...) so the change lands in the undo history — the system
   snapshots the current state and only records it if it differs from
   the previous checkpoint. Calling it before the edit does nothing.
4. **Loading adds, it does not replace.** `load_molecule_from_smiles`
   and friends add to the existing canvas. Call `clear_canvas` first
   when the user means "replace the molecule".
5. **File I/O is sandboxed.** All file tools need a base directory. If a
   file tool errors with "base directory is not configured", ask the
   user for a directory and call `set_file_io_config` — do not guess a
   path. Extensions are allowlisted; add new ones via the same tool.

## Tool map

| Goal | Tools |
|---|---|
| Inspect state | `get_current_molecule`, `get_molecule_xyz`, `get_atom_properties`, `get_bond_info`, `get_selected_atoms`, `get_mapped_smiles`, `get_app_info` |
| Load molecules | `load_molecule_by_name` (PubChem), `load_molecule_from_smiles`, `load_from_mol_block`, `show_xyz_in_viewer`, `clear_canvas` |
| Edit | `apply_reaction_smarts` (SMARTS transforms), `run_python` (arbitrary RDKit / PluginContext code), `push_undo_checkpoint`, `check_chemistry` |
| 3D view | `trigger_3d_conversion`, `enter_3d_mode` / `exit_3d_mode`, `set_cpk_color_override` (persists across redraws), `set_bond_color_override` (by bond index or `"i-j"` atom pairs), `reset_cpk_color_override`, `reset_3d_camera`, `refresh_3d_view`, `fit_2d_view`, `refresh_ui` |
| Files (sandboxed) | `write_file_with_xyz_block` (**preferred for inputs**), `write_text_file`, `read_text_file`, `list_directory`, `delete_file`, `get_file_io_config`, `set_file_io_config` |
| Plugin authoring | `get_plugin_dev_manual`, `list_app_source_tree`, `get_app_source`, `get_plugin_dir`, `reload_plugins` |
| Plugin discovery | `list_available_plugins` (official registry, optional `search`), `open_plugin_installer` |

When the user needs functionality MoleditPy lacks (an input generator,
analyzer, exporter, ...), check `list_available_plugins` first — an
official plugin may already exist. Suggest it, then call
`open_plugin_installer` so the user can install it in-app; never
download plugin code yourself. If the installer is unavailable, point
the user at the Plugin Explorer for a manual download:
https://hiroyokoyama.github.io/moleditpy-plugins/explorer/

## Recipe: generate a QM input file

1. `get_current_molecule` — confirm loaded; note formula for sanity.
2. If no 3D coords: `trigger_3d_conversion`.
3. `get_file_io_config` — if no base dir, ask the user, then
   `set_file_io_config`.
4. `write_file_with_xyz_block` with:
   - `path`: e.g. `"opt/job.inp"` (parents auto-created)
   - `header`: keywords + charge/multiplicity — pass an ARRAY of lines
     (e.g. ORCA: `["! B3LYP def2-TZVP Opt", "* xyz 0 1"]`); this avoids
     newline-escaping problems entirely
   - `footer`: closing section if the format needs one (ORCA: `["*"]`)
   - options if the format demands them: `element_style`
     (`symbol` default → `C x y z`; `atomic_number` → `6 x y z`;
     `symbol_and_number` → `C 6.0 x y z` for GAMESS `$DATA`),
     `atom_order` (0-based indices, reorder or subset), `precision`,
     `xyz_header`+`comment` for standard `.xyz` files.
5. `read_text_file` the result back and show the user the final content.

For multi-file jobs, repeat step 4 per file; use `overwrite: true` only
when the user asked to replace.

## Recipe: write a MoleditPy plugin

1. `get_plugin_dev_manual` for the V4 API contract.
2. `get_app_source` on `plugins/plugin_interface.py` for exact
   `PluginContext` signatures — do not invent API from memory.
3. `get_plugin_dir`, then point `set_file_io_config` at it and
   `write_text_file` the plugin (single `.py` with `initialize(context)`
   and the `PLUGIN_*` constants).
4. `reload_plugins` to activate without restarting.

## `run_python` escape hatch

Runs on the Qt main thread with the PluginContext available as `ctx`
(import RDKit yourself); stdout/stderr are captured, and assigning to
`result` returns a value (e.g. `result = ctx.current_molecule.GetNumAtoms()`).
Use it for anything the dedicated tools cannot do (batch atom edits,
custom descriptors). Keep snippets short; after mutating the molecule
call `ctx.refresh_ui()` / `ctx.refresh_3d_view()` and then
`push_undo_checkpoint`.
