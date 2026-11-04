// Read-only shared consumer for either A or B canonical records.
export function canonicalView(r){
 if(r?.contract!=='UTT-CANONICAL-1/1.0.0'||!r.identity?.record_id||!r.title||!r.state?.original||!r.content||!Array.isArray(r.content.display_blocks)||!Array.isArray(r.projects)||!Array.isArray(r.sources))throw Error('CANONICAL_CONSUMER_INVALID');
 return {id:r.identity.record_id,title:r.title,role:r.document_role,state:r.state,blocks:r.content.display_blocks,rawText:r.content.raw_text,
  auxiliary:r.content.auxiliary_blocks,projects:r.projects,sources:r.sources,lineage:r.lineage,classification:r.classification,
  provenance:r.provenance,parseStatus:r.parse_status,originalIds:r.identity.original_ids};
}
