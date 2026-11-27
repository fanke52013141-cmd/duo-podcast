/* Cloud-aware voice mutations.
 * Provider, model and voice ID survive into the frozen job snapshot.
 * Secrets are never accepted by these hooks.
 */
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { sendJson, type JobAccepted } from './api';

export interface VoiceBindingPayload {
  id: string;
  characterId: string;
  providerProfileId: string;
  modelId?: string;
  providerVoiceId?: string;
  minimaxVoiceId?: string;
  localRefAudio?: { artifactId: string; path: string; fileHash: string } | null;
}

export interface FlexibleVoiceSynthInput {
  projectId: string;
  clientToken: string;
  revisionId: string;
  voiceBindings: Record<string, VoiceBindingPayload>;
  transitionGapMs?: number[];
  turnIds?: string[];
}

export const useFlexibleVoiceSynthesize = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (input: FlexibleVoiceSynthInput) =>
      sendJson<JobAccepted>(`/projects/${input.projectId}/voice/synthesize`, 'POST', {
        clientToken: input.clientToken,
        revisionId: input.revisionId,
        voiceBindings: input.voiceBindings,
        transitionGapMs: input.transitionGapMs ?? [],
        ...(input.turnIds ? { turnIds: input.turnIds } : {}),
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['jobs'] }),
  });
};

export interface FlexibleVoiceAuditionInput {
  projectId: string;
  speaker: 'A' | 'B';
  text?: string;
  providerProfileId: string;
  modelId?: string;
  voiceId?: string;
  referenceAudio?: { artifactId: string; path: string; fileHash: string };
}

export const useFlexibleVoiceAudition = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (input: FlexibleVoiceAuditionInput) =>
      sendJson<JobAccepted>(`/projects/${input.projectId}/voice/audition`, 'POST', {
        speaker: input.speaker,
        text: input.text ?? '',
        referenceAudio: input.referenceAudio,
        providerProfileId: input.providerProfileId,
        modelId: input.modelId,
        voiceId: input.voiceId,
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['jobs'] }),
  });
};
