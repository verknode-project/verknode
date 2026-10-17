import { ReactWidget } from '@jupyterlab/apputils';
import { ABCWidgetFactory, DocumentRegistry, DocumentWidget } from '@jupyterlab/docregistry';
import { LabIcon } from '@jupyterlab/ui-components';
import '@xyflow/react/dist/style.css';
import React from 'react';

import { WorkflowController } from './controller';
import { WorkflowEditor } from './editor/Editor';
import { EditorSettings } from './settings';

/** The verknode icon (the secondary logo: a big light blue V cropped by a dark squircle, red / yellow / green bands right of it): files, tabs, launcher. */
export const VERKNODE_ICON_SVG = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="16" height="16"><defs><clipPath id="verknode-icon-clip"><rect width="24" height="24" rx="5"/></clipPath></defs><rect width="24" height="24" rx="5" fill="#1f2328"/><g clip-path="url(#verknode-icon-clip)"><path d="M20.13 -3L24.53 -3L8.76 27L4.36 27Z" fill="#F52D1A"/><path d="M24.53 -3L28.93 -3L13.16 27L8.76 27Z" fill="#FFC400"/><path d="M28.93 -3L60.13 -3L44.36 27L13.16 27Z" fill="#00B86A"/><path d="M-8.34 -1.5H-3.74L5.5 16.08L14.74 -1.5H19.34L5.5 24.83Z" fill="#5DA9E9"/></g></svg>`;

export const workflowIcon = new LabIcon({ name: 'verknode:workflow', svgstr: VERKNODE_ICON_SVG });

/**
 * The browser tab icon: verknode instead of Jupyter's. JupyterLab switches
 * between its idle and busy icon links (kernels running), so both get a
 * verknode version - busy with the red, yellow and green bars grey.
 */
export function setFavicon(): void {
  const uri = (svg: string) => `data:image/svg+xml,${encodeURIComponent(svg)}`;
  const idle = uri(VERKNODE_ICON_SVG);
  const busy = uri(VERKNODE_ICON_SVG.replace(/#F52D1A|#FFC400|#00B86A/g, '#9aa3ad'));
  const links = Array.from(document.querySelectorAll<HTMLLinkElement>('link.favicon, link[rel~="icon"]'));
  for (const link of links) {
    link.type = 'image/svg+xml';
    link.href = link.classList.contains('busy') ? busy : idle;
  }
  if (!links.length) {
    const link = document.createElement('link');
    link.rel = 'icon';
    link.type = 'image/svg+xml';
    link.href = idle;
    document.head.appendChild(link);
  }
}

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
