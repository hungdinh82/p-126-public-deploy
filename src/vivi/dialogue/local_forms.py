"""Local pending forms end when a new, independent topic takes the foreground."""


def close_obsolete_forms(session, semantic):
    if semantic.act in {'deny', 'cancel'}:
        state = session.model_copy(deep=True)
        state.clarification, state.dialogue_question = None, None
        return state
    if semantic.act not in {'request', 'question', 'offer', 'reply', 'unsupported'}:
        return session
    # Navigation selections/refinements continue their existing typed form;
    # they are not a new topic and still validate their checkpoint handles.
    continuations = {'navigation.select', 'navigation.refine', 'navigation.start',
                     'trip.select', 'trip.refine', 'trip.start'}
    if semantic.actions and all(action.capability in continuations for action in semantic.actions):
        return session
    state = session.model_copy(deep=True)
    for task in [state.active, *state.suspended]:
        if not task or task.current_index() is None:
            continue
        task.revision += 1
        for step in task.steps:
            # Executed effects and in-flight/unknown outcomes retain their ledger.
            if step.status not in {'completed', 'cancelled', 'running', 'unknown'}:
                step.status, step.confirmation = 'cancelled', None
    state.suspended = [task for task in state.suspended if task.current_index() is not None]
    state.clarification = None
    state.dialogue_question = None
    return state
