/**
 * Programmatic model API — `navig-menu/model`.
 *
 * The resolved, override- and plugin-applied menu model for a project — the same data
 * `navig-menu list --json` prints — for in-process consumers (e.g. a native command palette in
 * navig-os / navig-deck) that want the model without shelling out to the CLI. `./menu-builder` is
 * an *authoring* DSL; this is the *resolver*.
 *
 *   import { scanProject, loadDefinition, buildMenuModel } from "navig-menu/model";
 *   const model = buildMenuModel(scanProject(root), loadDefinition(root));
 *   // model.groups[].items[] → { id, label, launcher, argv, cwd, risk, description, project, … }
 */
export {
  scanProject,
  getManifest,
  loadDefinition,
  buildMenuModel,
  type MenuModel,
  type DefinitionLoad,
} from "./builder/build.js";
export type { Manifest, MenuDefinition, Action } from "./manifest/schema.js";
