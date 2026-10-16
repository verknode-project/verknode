import {
  ILayoutRestorer,
  JupyterFrontEnd,
  JupyterFrontEndPlugin
} from '@jupyterlab/application';
import { ICommandPalette, WidgetTracker } from '@jupyterlab/apputils';
import { INotebookTracker } from '@jupyterlab/notebook';
import { ILauncher } from '@jupyterlab/launcher';
import { ISettingRegistry } from '@jupyterlab/settingregistry';

import { WorkflowDocument, WorkflowFactory, workflowIcon } from './document';
import { EditorSettings } from './settings';
import { setLabHost } from './editor/labViews';
import { setNotebookFinder } from './liveNotebook';

const FACTORY = 'Verknode Workflow Editor';
const FILE_TYPE = 'vkf';

namespace CommandIDs {
  export const create = 'verknode:create-workflow';
}

const plugin: JupyterFrontEndPlugin<void> = {
  id: 'jupyterlab-verknode:plugin',
  description: 'Node-based visual programming for Python.',
  autoStart: true,
  optional: [ICommandPalette, ILauncher, ILayoutRestorer, ISettingRegistry, INotebookTracker],
  activate: async (
    app: JupyterFrontEnd,
    palette: ICommandPalette | null,
    launcher: ILauncher | null,
    restorer: ILayoutRestorer | null,
    settingRegistry: ISettingRegistry | null,
    notebooks: INotebookTracker | null
  ) => {
    // Jupyter Notebook nodes can run in their notebook when it is open (liveNotebook.ts)
    setNotebookFinder(path => notebooks?.find(panel => panel.context.path === path) ?? null);
    let pluginSettings: ISettingRegistry.ISettings | null = null;
    try {
      pluginSettings = settingRegistry ? await settingRegistry.load(plugin.id) : null;
    } catch (e) {
      console.warn('verknode: could not load editor settings', e);
    }
    const settings = new EditorSettings(pluginSettings);
    // port / node views as JupyterLab tabs, split next to the workflow editor
    setLabHost({
      add: widget => app.shell.add(widget, 'main', { mode: 'split-right', ref: app.shell.currentWidget?.id ?? null }),
      activate: id => app.shell.activateById(id),
      openFile: path => void app.commands.execute('docmanager:open', { path })
    });

    app.docRegistry.addFileType({
      name: FILE_TYPE,
      displayName: 'Verknode Workflow',
      extensions: ['.vkf'],
      mimeTypes: ['application/zip'],
      fileFormat: 'base64',
      contentType: 'file',
      icon: workflowIcon
    });

    const factory = new WorkflowFactory(settings, {
      name: FACTORY,
      label: 'Workflow Editor',
      modelName: 'base64',
      fileTypes: [FILE_TYPE],
      defaultFor: [FILE_TYPE]
    });
    const tracker = new WidgetTracker<WorkflowDocument>({ namespace: 'verknode-workflow' });
    factory.widgetCreated.connect((_, widget) => {
      widget.context.pathChanged.connect(() => void tracker.save(widget));
      void tracker.add(widget);
    });
    app.docRegistry.addWidgetFactory(factory);

    if (restorer) {
      void restorer.restore(tracker, {
        command: 'docmanager:open',
        args: widget => ({ path: widget.context.path, factory: FACTORY }),
        name: widget => widget.context.path
      });
    }

    app.commands.addCommand(CommandIDs.create, {
      label: args => (args['isLauncher'] ? 'Workflow' : 'New Verknode Workflow'),
      caption: 'Create a new workflow (.vkf)',
      icon: args => (args['isPalette'] ? undefined : workflowIcon),
      execute: async args => {
        const cwd = (args['cwd'] as string) ?? '';
        const model = await app.serviceManager.contents.newUntitled({ path: cwd, type: 'file', ext: '.vkf' });
        return app.commands.execute('docmanager:open', { path: model.path, factory: FACTORY });
      }
    });

    palette?.addItem({ command: CommandIDs.create, category: 'Verknode' });
    launcher?.add({ command: CommandIDs.create, category: 'Other', rank: 0 });
  }
};

export default plugin;
