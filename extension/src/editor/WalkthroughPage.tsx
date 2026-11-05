import React from 'react';

import { WorkflowController } from '../controller';
import { useWorkflowVersion, ViewFrame, ViewFrameKind, ViewMove } from './ChildWindow';
import { parseWalkthrough, Walkthrough, WalkthroughData } from './widgets/walkthrough';

/**
 * A walkthrough as a page of its own: a dialog, a browser window or tab, or a JupyterLab tab next to the
 * editor - stepping through it shows the parts of the workflow on the canvas.
 */
export function WalkthroughPage(props: {
  controller: WorkflowController;
  nodeId: number;
  onClose: () => void;
  frame?: ViewFrameKind;
  onTitle?: (title: string) => void;
  moves?: ViewMove[];
}) {
  const { controller, nodeId } = props;
  useWorkflowVersion(controller);
  const node = controller.view?.nodes.find(n => n.id === nodeId);
  const w = node?.widget;
  const data: WalkthroughData = (w?.type === 'output' && w.data) || {};
  const title = data.title || parseWalkthrough(data.document ?? '').title || node?.annotation || 'Walkthrough';
  return (
    <ViewFrame
      frame={props.frame ?? 'modal'}
      title={title}
      onClose={props.onClose}
      onTitle={props.onTitle}
      moves={props.moves}
      className="verknode-walk-frame"
    >
      {node ? (
        <Walkthrough nodeId={nodeId} data={data} page busy={node.state === 'EXECUTING' || node.state === 'QUEUED'} />
      ) : (
        <div className="verknode-empty">The walkthrough's node is not in the workflow any more.</div>
      )}
    </ViewFrame>
  );
}
