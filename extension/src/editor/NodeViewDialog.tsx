import React, { useEffect, useState } from 'react';

import { WorkflowController } from '../controller';
import { NodeViewData } from '../types';
import { useWorkflowVersion, ViewFrame, ViewFrameKind, ViewMove } from './ChildWindow';

/**
 * Node view ("Open View"): HTML rendered by the node, shown in a
 * sandboxed iframe without scripts.
 */
export function NodeViewDialog(props: {
  controller: WorkflowController;
  nodeId: number;
  onClose: () => void;
  frame?: ViewFrameKind;
  onTitle?: (title: string) => void;
  moves?: ViewMove[];
}) {
  const { controller, nodeId } = props;
  const version = useWorkflowVersion(controller);
  const [fresh, setFresh] = useState<NodeViewData | null>(null);
  // the last view stays visible (marked outdated) while the node is reset or runs
  const [last, setLast] = useState<NodeViewData | null>(null);
  const stale = !!fresh && !fresh.html && !!last;
  const data = stale ? last : fresh;
  const [error, setError] = useState<string | null>(null);
  const state = controller.view?.nodes.find(n => n.id === nodeId)?.state;
  const busy = state === 'EXECUTING' || state === 'QUEUED';

  useEffect(() => {
    controller
      .nodeView(nodeId)
      .then(d => {
        setFresh(d);
        if (d.html) {
          setLast(d);
        }
      })
      .catch(e => setError(e.message));
  }, [nodeId, version]);

  return (
    <ViewFrame
      frame={props.frame ?? 'modal'}
      title={data ? `View - ${data.nodeName} (#${nodeId})` : 'View'}
      onClose={props.onClose}
      onTitle={props.onTitle}
      moves={props.moves}
      className="verknode-nodeview"
    >
      {stale && (
        <div className="verknode-stale-note">
          Outdated: {busy ? 'the node is running - the view updates when it has executed.' : 'the node was reset - showing its last view until it runs again.'}
        </div>
      )}
      {error && <div className="verknode-dialog-error">{error}</div>}
      {data && !data.html && (
        <div className="verknode-empty">
          {busy ? 'Executing… the view opens when the node has executed.' : 'No view available. Execute the node first.'}
        </div>
      )}
      {data?.html && <iframe className="verknode-view-frame" sandbox="" srcDoc={data.html} title="Node view" />}
    </ViewFrame>
  );
}
