import { ReactWidget } from '@jupyterlab/apputils';
import { ABCWidgetFactory, DocumentRegistry, DocumentWidget } from '@jupyterlab/docregistry';
import { LabIcon } from '@jupyterlab/ui-components';
import '@xyflow/react/dist/style.css';
import React from 'react';

import { WorkflowController } from './controller';
import { WorkflowEditor } from './editor/Editor';
import { EditorSettings } from './settings';

export const workflowIcon = new LabIcon({
  name: 'verknode:workflow',
  svgstr: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="16">
  <g class="jp-icon3" fill="#616161">
    <rect x="1" y="3" width="7" height="7" rx="1.2" fill="#f1933b"/>
    <rect x="16" y="3" width="7" height="7" rx="1.2" fill="#fcd94d"/>
    <rect x="8.5" y="14" width="7" height="7" rx="1.2" fill="#2fa8c4"/>
    <path d="M8 6.5h8M4.5 10l5.5 4M19.5 10l-5.5 4" stroke="#424242" stroke-width="1.3" fill="none"/>
  </g>
</svg>`
});

export class WorkflowPanel extends ReactWidget {
  constructor(
    readonly controller: WorkflowController,
    readonly settings: EditorSettings
  ) {
    super();
    this.addClass('jp-VerknodeWorkflow');
  }

  render(): JSX.Element {
    return <WorkflowEditor controller={this.controller} settings={this.settings} />;
  }
}

export class WorkflowDocument extends DocumentWidget<WorkflowPanel> {
  dispose(): void {
    if (!this.isDisposed) {
      this.content.controller.dispose();
    }
    super.dispose();
  }
}

/**
 * Opens .vkf files in the workflow editor.
 */
export class WorkflowFactory extends ABCWidgetFactory<WorkflowDocument, DocumentRegistry.IModel> {
  constructor(
    private readonly settings: EditorSettings,
    options: DocumentRegistry.IWidgetFactoryOptions<WorkflowDocument>
  ) {
    super(options);
  }

  protected createNewWidget(context: DocumentRegistry.Context): WorkflowDocument {
    const controller = new WorkflowController(context);
    const widget = new WorkflowDocument({ content: new WorkflowPanel(controller, this.settings), context });
    widget.title.icon = workflowIcon;
    void controller.start();
    return widget;
  }
}
