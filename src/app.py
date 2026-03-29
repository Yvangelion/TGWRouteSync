import boto3
import json
import logging
import os
from botocore.exceptions import ClientError

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

def get_tgw_config():
    """
    Get TGW configuration from environment variable.
    
    Expected format (JSON array):
    [
        {"tgw_id": "tgw-xxx", "region": "ap-southeast-2"},
        {"tgw_id": "tgw-yyy", "region": "us-east-1"}
    ]
    """
    tgw_config_str = os.environ.get('TGW_CONFIG', '[]')
    
    try:
        tgw_config = json.loads(tgw_config_str)
        logger.info(f"Loaded TGW config with {len(tgw_config)} TGW(s)")
        return tgw_config
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse TGW_CONFIG: {e}")
        return []

def get_ec2_client(region):
    """
    Get EC2 client for a specific region.
    """
    return boto3.client('ec2', region_name=region)

def lambda_handler(event, context):
    """
    Main handler for TGW route sync.
    
    Supports multiple TGWs across different regions.
    
    Two modes:
    1. Event-driven: Triggered by EventBridge, processes the specific TGW from the event
    2. Scheduled: Processes all TGWs defined in TGW_CONFIG environment variable
    """
    logger.info(f"Received event: {json.dumps(event, indent=2)}")
    
    # Load TGW configuration
    tgw_config = get_tgw_config()
    
    try:
        # Check if this is an EventBridge event with TGW details
        if 'detail' in event and event.get('detail'):
            # Event-driven mode: Process the specific TGW from the event
            result = process_event_driven(event, tgw_config)
        else:
            # Scheduled mode: Process all configured TGWs
            result = process_all_tgws(tgw_config)
        
        return result
        
    except Exception as e:
        logger.error(f"Error during route sync: {str(e)}", exc_info=True)
        return {'statusCode': 500, 'body': f'Error: {str(e)}'}

def process_event_driven(event, tgw_config):
    """
    Process a single TGW based on EventBridge event.
    Extracts TGW info from the event and finds matching config.
    """
    detail = event.get('detail', {})
    
    # Extract TGW ID from event
    tgw_arn = detail.get('transitGatewayArn', '')
    event_tgw_id = tgw_arn.split('/')[-1] if tgw_arn else None
    
    # Extract region from event
    event_region = detail.get('region', '')
    
    # Extract TGW route table ARNs from event
    tgw_rt_arns = detail.get('transitGatewayRouteTableArns', [])
    
    if not event_tgw_id:
        logger.error("Could not extract TGW ID from event")
        return {'statusCode': 400, 'body': 'Missing TGW ID in event'}
    
    if not event_region:
        logger.error("Could not extract region from event")
        return {'statusCode': 400, 'body': 'Missing region in event'}
    
    logger.info(f"Event-driven mode: TGW {event_tgw_id} in {event_region}")
    
    # Verify this TGW is in our config (security check)
    tgw_in_config = any(
        cfg.get('tgw_id') == event_tgw_id and cfg.get('region') == event_region 
        for cfg in tgw_config
    )
    
    if not tgw_in_config:
        logger.warning(f"TGW {event_tgw_id} in {event_region} not in TGW_CONFIG - skipping")
        return {
            'statusCode': 200, 
            'body': f'TGW {event_tgw_id} not in configured TGW list - skipped'
        }
    
    # Process each route table from the event
    results = []
    for tgw_rt_arn in tgw_rt_arns:
        tgw_rt_id = tgw_rt_arn.split('/')[-1]
        logger.info(f"Processing TGW route table: {tgw_rt_id}")
        
        result = process_tgw_route_table(event_tgw_id, tgw_rt_id, event_region)
        results.append(result)
    
    return {
        'statusCode': 200,
        'body': {
            'mode': 'event-driven',
            'tgw_id': event_tgw_id,
            'region': event_region,
            'route_tables_processed': len(results),
            'results': results
        }
    }

def process_all_tgws(tgw_config):
    """
    Process all TGWs defined in configuration (scheduled mode).
    """
    if not tgw_config:
        logger.warning("No TGWs configured in TGW_CONFIG")
        return {'statusCode': 200, 'body': 'No TGWs configured'}
    
    logger.info(f"Scheduled mode: Processing {len(tgw_config)} TGW(s)")
    
    all_results = []
    
    for config in tgw_config:
        tgw_id = config.get('tgw_id')
        region = config.get('region')
        
        if not tgw_id or not region:
            logger.warning(f"Invalid config entry: {config}")
            continue
        
        logger.info(f"Processing TGW: {tgw_id} in {region}")
        
        # Get all route tables for this TGW
        ec2 = get_ec2_client(region)
        
        try:
            response = ec2.describe_transit_gateway_route_tables(
                Filters=[
                    {'Name': 'transit-gateway-id', 'Values': [tgw_id]}
                ]
            )
            
            route_tables = response.get('TransitGatewayRouteTables', [])
            logger.info(f"Found {len(route_tables)} route tables for TGW {tgw_id}")
            
            for rt in route_tables:
                tgw_rt_id = rt['TransitGatewayRouteTableId']
                result = process_tgw_route_table(tgw_id, tgw_rt_id, region)
                all_results.append(result)
                
        except ClientError as e:
            logger.error(f"Error getting route tables for TGW {tgw_id}: {e}")
            all_results.append({
                'tgw_id': tgw_id,
                'region': region,
                'status': 'error',
                'error': str(e)
            })
    
    # Summary
    total_added = sum(r.get('total_routes_added', 0) for r in all_results if isinstance(r.get('total_routes_added'), int))
    total_removed = sum(r.get('total_routes_removed', 0) for r in all_results if isinstance(r.get('total_routes_removed'), int))
    
    return {
        'statusCode': 200,
        'body': {
            'mode': 'scheduled',
            'tgws_processed': len(tgw_config),
            'route_tables_processed': len(all_results),
            'total_routes_added': total_added,
            'total_routes_removed': total_removed,
            'results': all_results
        }
    }

def process_tgw_route_table(tgw_id, tgw_route_table_id, region):
    """
    Process a single TGW route table - sync routes to tagged VPC route tables.
    """
    logger.info(f"Processing TGW route table: {tgw_route_table_id} (TGW: {tgw_id}, Region: {region})")
    
    ec2 = get_ec2_client(region)
    
    result = {
        'tgw_id': tgw_id,
        'tgw_route_table_id': tgw_route_table_id,
        'region': region,
        'status': 'success',
        'vpc_route_tables_synced': 0,
        'total_routes_added': 0,
        'total_routes_removed': 0,
        'details': []
    }
    
    try:
        # Step 1: Get all routes from the TGW route table
        tgw_routes = get_tgw_routes(ec2, tgw_route_table_id)
        logger.info(f"Found {len(tgw_routes)} active routes in TGW route table")
        
        for route in tgw_routes:
            logger.info(f"  TGW Route: {route.get('DestinationCidrBlock')} | Type: {route.get('Type')}")
        
        # Step 2: Get VPC attachments associated with this TGW route table
        vpc_ids = get_associated_vpc_ids(ec2, tgw_route_table_id)
        logger.info(f"Found {len(vpc_ids)} VPCs associated with TGW route table")
        
        if not vpc_ids:
            logger.warning("No VPC attachments found for this TGW route table")
            result['status'] = 'no_vpcs'
            return result
        
        # Step 3: Find route tables tagged for sync in those VPCs
        target_route_tables = discover_tagged_route_tables(ec2, vpc_ids)
        logger.info(f"Found {len(target_route_tables)} route tables tagged for sync")
        
        if not target_route_tables:
            logger.warning("No route tables found with TGWRouteSync=enabled tag")
            result['status'] = 'no_tagged_route_tables'
            return result
        
        # Step 4: Sync routes to each target route table
        for rt_id in target_route_tables:
            sync_result = sync_routes_to_vpc_route_table(ec2, rt_id, tgw_routes, tgw_id)
            result['details'].append(sync_result)
            result['total_routes_added'] += len(sync_result.get('added', []))
            result['total_routes_removed'] += len(sync_result.get('removed', []))
        
        result['vpc_route_tables_synced'] = len(target_route_tables)
        
        logger.info(f"Sync completed for {tgw_route_table_id}: {result['total_routes_added']} added, {result['total_routes_removed']} removed")
        
        return result
        
    except Exception as e:
        logger.error(f"Error processing TGW route table {tgw_route_table_id}: {e}", exc_info=True)
        result['status'] = 'error'
        result['error'] = str(e)
        return result

def get_tgw_routes(ec2, tgw_route_table_id):
    """
    Fetch all active routes from the TGW route table.
    """
    routes = []
    
    try:
        response = ec2.search_transit_gateway_routes(
            TransitGatewayRouteTableId=tgw_route_table_id,
            Filters=[
                {'Name': 'state', 'Values': ['active']}
            ],
            MaxResults=1000
        )
        
        all_routes = response.get('Routes', [])
        
        # Filter to only routes with CIDR blocks
        routes = [r for r in all_routes if 'DestinationCidrBlock' in r]
        
        logger.info(f"Retrieved {len(routes)} CIDR-based routes from TGW route table")
        
        return routes
        
    except ClientError as e:
        logger.error(f"Error fetching TGW routes: {e}")
        return []

def get_associated_vpc_ids(ec2, tgw_route_table_id):
    """
    Get all VPC IDs that have attachments ASSOCIATED with this TGW route table.
    """
    vpc_ids = []
    
    try:
        # Get all associations for this TGW route table
        response = ec2.get_transit_gateway_route_table_associations(
            TransitGatewayRouteTableId=tgw_route_table_id
        )
        
        associations = response.get('Associations', [])
        logger.info(f"Found {len(associations)} total associations for TGW route table")
        
        # Filter to only VPC attachments
        vpc_attachment_ids = []
        for assoc in associations:
            resource_type = assoc.get('ResourceType', '')
            attachment_id = assoc.get('TransitGatewayAttachmentId', '')
            
            logger.info(f"  Association: {attachment_id} | Type: {resource_type}")
            
            if resource_type == 'vpc':
                vpc_attachment_ids.append(attachment_id)
        
        if not vpc_attachment_ids:
            logger.info("No VPC attachments associated with this TGW route table")
            return []
        
        logger.info(f"Found {len(vpc_attachment_ids)} VPC attachments")
        
        # Get VPC IDs from the attachments
        attachment_response = ec2.describe_transit_gateway_vpc_attachments(
            TransitGatewayAttachmentIds=vpc_attachment_ids
        )
        
        for attachment in attachment_response.get('TransitGatewayVpcAttachments', []):
            state = attachment.get('State', '')
            vpc_id = attachment.get('VpcId', '')
            
            if state == 'available':
                vpc_ids.append(vpc_id)
                logger.info(f"  VPC attachment available: {vpc_id}")
            else:
                logger.warning(f"  VPC attachment not available: {vpc_id} (state: {state})")
        
        return vpc_ids
        
    except ClientError as e:
        logger.error(f"Error getting VPC associations: {e}")
        return []

def discover_tagged_route_tables(ec2, vpc_ids):
    """
    Find all route tables in the specified VPCs that are tagged for TGW route sync.
    
    Required tag: TGWRouteSync = "enabled" (or "true" or "yes")
    """
    route_table_ids = []
    
    try:
        response = ec2.describe_route_tables(
            Filters=[
                {'Name': 'vpc-id', 'Values': vpc_ids},
                {'Name': 'tag:TGWRouteSync', 'Values': ['enabled', 'true', 'yes']}
            ]
        )
        
        for rt in response.get('RouteTables', []):
            rt_id = rt['RouteTableId']
            vpc_id = rt['VpcId']
            route_table_ids.append(rt_id)
            logger.info(f"  Discovered tagged route table: {rt_id} in VPC {vpc_id}")
        
        return route_table_ids
        
    except ClientError as e:
        logger.error(f"Error discovering tagged route tables: {e}")
        return []

def sync_routes_to_vpc_route_table(ec2, vpc_rt_id, tgw_routes, tgw_id):
    """
    Synchronize routes from TGW route table to a VPC route table.
    
    Uses state-based comparison (by CIDR):
    - Routes in TGW but not in VPC → Add
    - Routes in VPC (via TGW) but not in TGW → Remove
    """
    result = {
        'route_table': vpc_rt_id,
        'status': 'success',
        'added': [],
        'removed': [],
        'errors': []
    }
    
    try:
        # Get current routes in VPC route table
        response = ec2.describe_route_tables(RouteTableIds=[vpc_rt_id])
        
        if not response['RouteTables']:
            result['status'] = 'error'
            result['errors'].append(f'Route table {vpc_rt_id} not found')
            return result
        
        vpc_routes = response['RouteTables'][0].get('Routes', [])
        
        # Filter to only TGW-managed routes
        vpc_tgw_routes = [
            r for r in vpc_routes 
            if r.get('TransitGatewayId') == tgw_id
        ]
        
        logger.info(f"Route table {vpc_rt_id}: {len(vpc_routes)} total routes, {len(vpc_tgw_routes)} TGW-managed routes")
        
        # Build CIDR lookup sets
        tgw_cidrs = {r['DestinationCidrBlock'] for r in tgw_routes}
        vpc_cidrs = {r['DestinationCidrBlock'] for r in vpc_tgw_routes}
        
        # Calculate delta
        to_add = tgw_cidrs - vpc_cidrs
        to_remove = vpc_cidrs - tgw_cidrs
        
        logger.info(f"  Delta: {len(to_add)} to add, {len(to_remove)} to remove")
        
        # Add missing routes
        for cidr in to_add:
            try:
                ec2.create_route(
                    RouteTableId=vpc_rt_id,
                    DestinationCidrBlock=cidr,
                    TransitGatewayId=tgw_id
                )
                result['added'].append(cidr)
                logger.info(f"    Added route: {cidr} → {tgw_id}")
                
            except ClientError as e:
                error_code = e.response.get('Error', {}).get('Code', '')
                
                if error_code == 'RouteAlreadyExists':
                    try:
                        ec2.replace_route(
                            RouteTableId=vpc_rt_id,
                            DestinationCidrBlock=cidr,
                            TransitGatewayId=tgw_id
                        )
                        result['added'].append(cidr)
                        logger.info(f"    Replaced route: {cidr} → {tgw_id}")
                    except ClientError as replace_error:
                        error_msg = f"Failed to replace {cidr}: {str(replace_error)}"
                        result['errors'].append(error_msg)
                        logger.error(f"    {error_msg}")
                else:
                    error_msg = f"Failed to add {cidr}: {str(e)}"
                    result['errors'].append(error_msg)
                    logger.error(f"    {error_msg}")
        
        # Remove stale routes
        for cidr in to_remove:
            try:
                ec2.delete_route(
                    RouteTableId=vpc_rt_id,
                    DestinationCidrBlock=cidr
                )
                result['removed'].append(cidr)
                logger.info(f"    Removed stale route: {cidr}")
                
            except ClientError as e:
                error_msg = f"Failed to remove {cidr}: {str(e)}"
                result['errors'].append(error_msg)
                logger.error(f"    {error_msg}")
        
        if result['errors']:
            result['status'] = 'partial'
        
        return result
        
    except ClientError as e:
        result['status'] = 'error'
        result['errors'].append(str(e))
        logger.error(f"Error syncing route table {vpc_rt_id}: {e}")
        return result
