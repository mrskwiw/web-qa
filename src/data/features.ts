export type FeatureType =
  | 'auth' | 'billing' | 'character' | 'media' | 'messaging'
  | 'moderation' | 'notification' | 'onboarding' | 'settings' | 'analytics';

export type FeatureStatus = 'not-started' | 'in-progress' | 'complete';

export interface Feature {
  id: string;
  name: string;
  description: string;
  subsystem: string;
  type: FeatureType;
  dependsOn: string[];
  status: FeatureStatus;
  version?: string;
}

export const features: Feature[] = [];
