export interface DramaLook {
  id: number
  revision: number
  character_id: number
  name: string
  styling: string
  image_prompt: string
  is_default: boolean
}

export interface DramaCharacter {
  id: number
  revision: number
  name: string
  role: string
  description: string
  appearance: string
  novel_character_id: number | null
  novel_character_name: string | null
  looks: DramaLook[]
}

export interface DramaScene {
  id: number
  revision: number
  location: string
  time_of_day: string
  description: string
  lighting: string
  image_prompt: string
  novel_world_setting_id: number | null
  novel_world_setting_title: string | null
}

export interface DramaProp {
  id: number
  revision: number
  name: string
  category: string
  description: string
  image_prompt: string
}

export interface DramaAssets {
  characters: DramaCharacter[]
  scenes: DramaScene[]
  props: DramaProp[]
}

export type AssetKind = 'characters' | 'looks' | 'scenes' | 'props'
export type MemberKind = Exclude<AssetKind, 'characters'>
export type DramaAsset = DramaCharacter | DramaLook | DramaScene | DramaProp
export type AssetFields = Record<string, string | number | boolean | null>
export interface EpisodeVersion {
  id: number
  revision: number
}

export const assetLabel: Record<AssetKind, string> = {
  characters: '角色',
  looks: '造型',
  scenes: '场景',
  props: '道具',
}
export const promptType = { looks: 'look', scenes: 'scene', props: 'prop' } as const
export function assetName(row: DramaAsset) {
  return 'location' in row ? [row.location, row.time_of_day].filter(Boolean).join(' · ') : row.name
}

export function assetsOfKind(assets: DramaAssets, kind: AssetKind): DramaAsset[] {
  return kind === 'looks' ? assets.characters.flatMap(({ looks }) => looks) : assets[kind]
}
